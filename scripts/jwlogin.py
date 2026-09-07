"""正方教务（jwglxt）自动登录与课表抓取。

复现浏览器流程（详见 README / docs）：
  1. GET  /xtgl/login_slogin.html           取 JSESSIONID/route + 页面 csrftoken
  2. GET  /xtgl/login_getPublicKey.html     取 RSA 公钥 (modulus/exponent, base64)
  3. POST /xtgl/login_slogin.html?time=..   用户名 + RSA(PKCS#1 v1.5) 加密密码
  4. GET  /kbcx/xskbcx_cxXskbcxIndex.html   校验登录成功，并读取当前学年/学期(选中项)
  5. POST /kbcx/xskbcx_cxXsgrkb.html        取课表 JSON
  6. POST /kbcx/xskbcx_cxRjc.html           取节次->起止时间

密码加密（与页面 login.js 等价，见该文件）：mmsfjm=1 时页面用 jsbn 自带 RSA：
    rsaKey.setPublic(b64tohex(modulus), b64tohex(exponent));
    enPassword = hex2b64(rsaKey.encrypt(password));
即 PKCS#1 v1.5 类型2填充的 RSA 加密，输出 base64。这里用 Python 标准库
大整数直接实现，避免引入加密库。
"""
from __future__ import annotations

import base64
import os
import re
import time

__all__ = [
    "LoginError",
    "rsa_encrypt_b64",
    "ZhengfangClient",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


class LoginError(RuntimeError):
    """登录失败（含验证码触发、密码错误、会话未建立等）。"""


# --------------------------------------------------------------------------
# 纯 Python 实现 PKCS#1 v1.5 RSA 加密（零依赖）
# --------------------------------------------------------------------------
def rsa_encrypt_b64(modulus_b64: str, exponent_b64: str, plaintext: str) -> str:
    """用公钥 (base64 的 modulus/exponent) 加密，返回 base64 密文。"""
    n = int.from_bytes(base64.b64decode(modulus_b64), "big")
    e = int.from_bytes(base64.b64decode(exponent_b64), "big")
    k = (n.bit_length() + 7) // 8  # 模长字节数，本系统为 1024 bit -> 128 字节

    msg = plaintext.encode("utf-8")
    mLen = len(msg)
    psLen = k - mLen - 3
    if psLen < 8:
        raise LoginError("密码过长，无法用当前密钥加密")
    # 类型 2 填充：0x00 0x02 PS 0x00 M，PS 为不含 0 的随机字节
    while True:
        ps = os.urandom(psLen)
        if all(b != 0 for b in ps):
            break
    em = b"\x00\x02" + ps + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode("ascii")


# --------------------------------------------------------------------------
# 教务系统客户端
# --------------------------------------------------------------------------
class ZhengfangClient:
    def __init__(self, base_url: str, timeout: float = 20.0, max_retries: int = 3):
        import requests  # 惰性导入，离线/纯生成路径无需 requests

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _USER_AGENT
        self.session.headers["Referer"] = self.base_url + "/xtgl/login_slogin.html"
        self.csrftoken = ""
        self.xnm: str | None = None
        self.xqm: str | None = None

    # -- 底层请求 ---------------------------------------------------------
    def _request(self, method: str, path: str, **kw):
        import requests

        kw.setdefault("timeout", self.timeout)
        url = self.base_url + path
        last_err = None
        for _ in range(self.max_retries):
            try:
                return self.session.request(method, url, **kw)
            except requests.RequestException as e:  # 连接抖动重试
                last_err = e
                time.sleep(1)
        raise LoginError(f"网络请求失败: {url} ({last_err})")

    # -- 登录 -------------------------------------------------------------
    def _get_csrftoken(self, html: str) -> str:
        m = re.search(
            r'name=["\']csrftoken["\'][^>]*value=["\']([^"\']+)["\']', html
        ) or re.search(
            r'value=["\']([0-9a-fA-F,-]+)["\'][^>]*name=["\']csrftoken["\']', html
        )
        if not m:
            raise LoginError("登录页未找到 csrftoken（页面结构可能已变）")
        return m.group(1)

    def login(self, username: str, password: str) -> None:
        """完整登录；成功后 self.session 已带登录态 Cookie。"""
        # 1) 登录页
        r = self._request("GET", "/xtgl/login_slogin.html")
        if r.status_code >= 400:
            raise LoginError(f"访问登录页失败 HTTP {r.status_code}")
        self.csrftoken = self._get_csrftoken(r.text)

        # 2) 公钥
        r = self._request(
            "GET",
            "/xtgl/login_getPublicKey.html?time=%d" % int(time.time() * 1000),
        )
        key = r.json()
        modulus = key["modulus"]
        exponent = key["exponent"]

        # 3) 提交登录（密码被提交两次：页面输入框 + 隐藏的 #hidMm 同名框，键序固定）
        mm = rsa_encrypt_b64(modulus, exponent, password)
        data = [
            ("csrftoken", self.csrftoken),
            ("language", "zh_CN"),
            ("ydType", ""),
            ("yhm", username),
            ("mm", mm),
            ("mm", mm),
        ]
        r = self._request(
            "POST",
            "/xtgl/login_slogin.html?time=%d" % int(time.time() * 1000),
            data=data,
            allow_redirects=False,
        )
        # 2xx/3xx 都继续，真正的校验在下一步——去取业务页，能拿到业务内容才算成功
        # 4) 校验 + 读当前学期
        self._load_current_term()

    def _load_current_term(self) -> None:
        r = self._request(
            "GET",
            "/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N253508&layout=default",
        )
        text = r.text
        if "xskbcx_cxXsgrkb" not in text or 'name="xnm"' not in text:
            # 未登录：会被重定向回登录页
            raise LoginError(
                "登录失败：未进入课表页（可能密码错误、账号被锁定、"
                "或触发验证码）。请到浏览器确认后重试。"
            )
        xnm = re.search(r'name="xnm"[^>]*>.*?value="(\d+)"[^>]*selected', text, re.S)
        xqm = re.search(r'name="xqm"[^>]*>.*?value="(\d+)"[^>]*selected', text, re.S)
        if not (xnm and xqm):
            raise LoginError("未能从课表页解析当前学年/学期，请手动在配置里指定 term")
        self.xnm, self.xqm = xnm.group(1), xqm.group(1)

    # -- 课表数据 ---------------------------------------------------------
    def fetch_schedule(self, xnm: str, xqm: str) -> dict:
        data = {
            "xnm": xnm,
            "xqm": xqm,
            "kzlx": "ck",
            "xsdm": "",
            "kclbdm": "",
            "kclxdm": "",
        }
        r = self._request(
            "POST", "/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N253508", data=data
        )
        return r.json()

    def fetch_section_times(self, xnm: str, xqm: str, xqh_id: str = "00001") -> list[dict]:
        r = self._request(
            "POST",
            "/kbcx/xskbcx_cxRjc.html?gnmkdm=N253508",
            data={"xnm": xnm, "xqm": xqm, "xqh_id": xqh_id},
        )
        return r.json()
