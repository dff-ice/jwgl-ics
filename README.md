# 教务课表 → .ics 自动同步

每天登录正方教务系统（湖北工程学院 `jwgl.hbeu.edu.cn`），抓取个人课表，按**单双周/换教室/换节次**展开成 `.ics` 日历，自动提交并由 **GitHub Pages** 提供可订阅的日历地址。

> 只用于你本人课表。凭据只存 GitHub Secrets，不入库。节日调休暂不处理。

---

## 功能

- ✅ 自动登录（模拟正方 v5 前端：csrftoken + RSA 公钥加密 + 双份 `mm` 字段），自动识别当前学年/学期
- ✅ 课表 JSON（`kbList`）→ ICS，支持：
  - 周次 `1-16周`、`13-16周`、`1-8周,10-16周`
  - 单双周 `2-4周(双)` / `3-5周(单)` —— 各自落到真实日期，互不重叠
  - 一次连上多节（`5-8`、`1-4`），起止时间取首末节次（节次→时间表来自 `cxRjc` 实时下发）
- ✅ 线上课忽略：只取有排课的 `kbList`，无教室/UOOC 线上课不进日历
- ✅ **稳定 UID 防重复**：UID = `学期+课程代码+星期+节次+周号`，**不含教室/教师**；被调课/换地点的旧事件以 `STATUS:CANCELLED` 发出，订阅端不会残留重复
- ✅ GitHub Actions 每日定时 + 手动触发，内容无变化不产生新提交

## 目录结构

```
jwgl-ics/
├─ config.json               # base_url / 学期第1周周一 / 排除课程等
├─ course.ics                # 生成的日历（提交，供 Pages 发布/订阅）
├─ state.json                # 上次运行的事件清单（用于 diff 出 CANCELLED）
├─ index.html                # Pages 首页：订阅地址与使用说明
├─ .github/workflows/sync.yml
├─ scripts/
│  ├─ sync_course.py         # 入口
│  ├─ kbmodel.py             # 课表 JSON → 课程格子（纯逻辑，零依赖）
│  ├─ icsgen.py              # 周展开 / UID / 折行 / 生成 .ics（纯逻辑）
│  └─ jwlogin.py             # 自动登录 + 抓课表 + 节次时间（requests）
└─ tests/                    # 单元测试（unittest，无需额外依赖）
```

## 抓包得到的请求流程（供维护参考）

| # | 请求 | 说明 |
|---|---|---|
| 1 | `GET /xtgl/login_slogin.html` | 取 `JSESSIONID`/`route` Cookie 与页面 `csrftoken`（`uuid,uuid去横线`） |
| 2 | `GET /xtgl/login_getPublicKey.html` | 返回 RSA 公钥 `modulus`/`exponent`（base64） |
| 3 | `POST /xtgl/login_slogin.html?time=…` | `csrftoken & language=zh_CN & ydType= & yhm=学号 & mm=RSA密文 & mm=…`（mm 两次） |
| 4 | `GET /kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N253508` | 校验登录成功，并读当前 `xnm/xqm`（下拉框选中项） |
| 5 | `POST /kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N253508` | `xnm=…&xqm=…&kzlx=ck&xsdm=&kclbdm=&kclxdm=` → 课表 JSON（`kbList`） |
| 6 | `POST /kbcx/xskbcx_cxRjc.html?gnmkdm=N253508` | `xnm/xqm/xqh_id=00001` → 节次→起止时间 |

密码加密等价前端 `login.js`：`RSAKey.setPublic(b64tohex(modulus), b64tohex(exponent))` → `hex2b64(rsaKey.encrypt(password))`，即 **PKCS#1 v1.5** 加密后 base64（脚本用 Python 大整数纯实现，无第三方加密库）。

## 本地运行

```bash
# 1) 在线同步（真实登录，凭据从环境变量读）
pip install requests
export JW_USERNAME=你的学号
export JW_PASSWORD=你的密码
python scripts/sync_course.py            # 读 config.json，写 course.ics / state.json

# 2) 离线/演示（读已保存的抓包 JSON）
python scripts/sync_course.py \
  --input-kb tests/fixtures/anon_kb.json \
  --input-rjc tests/fixtures/anon_rjc.json

# 单元测试（无需 requests）
python -m unittest discover -s tests
```

**关键配置 `config.json`：**

- `semester_start`：**每学期开学前第 1 周周一**（本仓库按 2026-2027-1 学期 = `2026-09-07` 生成）。换学期记得改，或在线模式会自动探测学年学期、但第 1 周周一仍需手动维护。
- `term.xnm/xqm`：留 `null` 时在线自动探测；如需固定可手动指定。
- `exclude_courses`：按课程名忽略（可选）。
- `ignore_no_room`：`true` 表示无教室行视为线上/无固定排课，忽略。

## GitHub 部署（Pages + Actions）

1. 推送本仓库到 GitHub。
2. 仓库 **Settings → Secrets and variables → Actions**，新增两个 secret：
   - `JW_USERNAME` = 学号
   - `JW_PASSWORD` = 登录密码
3. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，分支 `main`，目录 `/ (root)`。
4. Actions 每天 UTC 21:07（北京 05:07）自动运行；也可在 Actions 页手动 `Run workflow` 立即同步。
5. 日历订阅地址：`https://<你的账号>.github.io/<仓库名>/course.ics`
   把该地址粘贴到 iOS/Google/Outlook 的"订阅日历"。更新会自动推送。

## 注意事项 / 边界

- **隐私**：Pages 订阅要求仓库公开（免费方案），即**你的课表会公开**。介意的话请勿公开仓库，改用本地定时运行后手动推送，或用支持私有 Pages 的付费方案。
- **验证码**：正常密码不会触发；多次输错会被锁定并弹验证码，届时 Actions 会失败并在日志提示。
- **节假日**：暂按教务周次直接展开，未处理国庆/调休等。需要时可在生成后做"跳过周/日期覆盖"。
- **网络**：若 GitHub 出口 IP 被学校风控拦截，可改在本地定时运行（Windows 任务计划）后 `git push`。
- 学校系统升级改接口后需对照上述请求流程更新 `scripts/jwlogin.py`。

## 免责声明

本项目仅用于个人学习与日常便利，请遵守学校相关规定，控制调用频率。
