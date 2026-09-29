# 低温磁阱电流安全审计（coil-audit）

在调整多个线圈电流前，确认安全约束、电源约束与场强线性上界能否**同时**满足。
系统对用户录入的整数不等式 `a·x ≤ b` 给出可复核的精确结论：

* **可行** → 由固定枢轴规则（Bland 规则）得到的一组**有理电流**，以及每条约束的
  **精确余量** `b − a·x`（分数形式，无浮点误差）；
* **无解** → 非负有理 Farkas 乘子，使各约束左侧系数逐项相加为零、右侧和严格为负，
  合并出矛盾式 `0 ≤ -k`，并逐项展示加权合并过程。

工程师读取一条**已冻结的无解**审计后，可携带稳定修复标识（`repair_id`）发起
**最小上界放宽审计**：求每条原约束右端应增加的非负有理量 `d_i ≥ 0`
（`a·x ≤ b` 变为 `a·x ≤ b + d_i`），使系统**首次可行**：

1. 先用精确有理 LP **最小化全部放宽量之和** `T = Σ d_i`；
2. 在所有 `Σ d_i = T` 的方案中，再按**稳定约束标识序**（`stable: true`
   的约束依原约束顺序排列）**字典序最小化**放宽向量。

返回放宽后的有理电流、逐约束新余量 `(b_i+d_i) − a·x` 与证明 `T` 最优的
**有界对偶乘子** `λ`：逐项 `0 ≤ λ_i ≤ 1`、按每个原变量
`Σ_i λ_i·a_{ij} = 0`（即 `Aᵀλ = 0`）、且
`−Σ_i λ_i·b_i = T`。放宽修复独立冻结，来源审计的规范变量、约束顺序与原证据
只读快照、永不改写；来源不存在、来源并非无解或同一修复标识改换来源时一律拒绝。

核心算法是**任意精度有理数单纯形**（Python `fractions.Fraction`），
不使用浮点、采样、外部线性规划服务，也不会在无解时只报告“无解”而不给证据。

## 运行

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up --build
# 浏览器打开 http://localhost:9090
```

健康状态：`GET /healthz` → `{"status":"ok",...}`（Compose 以该端点作为
服务健康条件）。冻结记录持久化在命名卷 `audit-data` 中。

### verify 单次容器

```bash
make verify          # = docker compose build
                     #   && docker compose up --abort-on-container-exit \
                     #                      --exit-code-from verify
echo $?              # 0 表示全部通过
```

`verify` 容器（`restart: "no"`）在 web 健康后运行一次并退出：

1. 容器内执行全部代码测试（pytest）；
2. HTTP 冒烟：健康检查、首页、未知编号返回 404；
3. 用两组“相加得 `0 ≤ -1`”的约束（`0·x ≤ -1`；`x ≤ 0` 与 `-x ≤ -1`）
   经真实 HTTP 提交，独立重算并核对 `μ ≥ 0`、`Σμ·a = 0`、`Σμ·b = -1`，
   同时核对幂等重放（200，同一记录）与改动载荷冲突（409，原证据不变）；
4. 再提交一个可行系统核对有理解与余量；
5. 经真实 HTTP 接口对三个矛盾组（含 `Σd = 4/3`、`λ = (1,1,2/3)` 的分数组）
   重算最小上界放宽：逐项核对 `d_i ≥ 0`、放宽后原语可行与逐约束新余量，
   独立重算 `0 ≤ λ_i ≤ 1`、`Σλ·a = 0`、`−Σλ·b = Σd`、稳定标识字典序，
   并核对修复幂等重放（200）、同标识改换来源（409，已冻结修复不变）、
   来源不存在（404）与来源可行（409，原审计不变）。

任一步失败即以非零码退出；全部通过输出 `VERIFY OK` 并以 **0** 退出。
镜像构建本身由 `make verify` 的 `docker compose build` 完成，失败同样中止。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计（JSON 见下） |
| `GET`  | `/api/audits/{audit_id}` | 按编号重新读取冻结结果 |
| `POST` | `/api/repairs` | 在已冻结的无解审计上发起最小上界放宽修复 |
| `GET`  | `/api/repairs/{repair_id}` | 按修复编号重新读取冻结修复 |
| `GET`  | `/healthz` | 健康检查 |

提交载荷（1–8 个变量，1–48 条约束；系数与右端必须是 **JSON 整数**，
`1.0`、`true`、`"3"` 一律拒绝）：

```json
{
  "audit_id": "trap-run-2026-09-28-A",
  "variables": ["I1", "I2"],
  "constraints": [
    {"coeffs": [1, 1], "b": 40, "label": "电源总电流上限", "stable": true},
    {"coeffs": [2, -1], "b": 10, "label": "场强线性上界", "stable": false}
  ]
}
```

* 首次提交 → `201`，结论与载荷指纹一起冻结落盘；
* 相同编号 + **完全相同载荷**重传 → `200`，返回同一条冻结记录（含相同
  `created_at` 与 `fingerprint`）；
* 相同编号 + **改动过的载荷**（任何系数/右端/标签/稳定标识/变量名变化）
  → `409 Conflict`，响应给出新旧指纹，**原证据不变**；
* 非法载荷 → `400`，不求解、不写入，任何旧结论都不会残留或被覆盖。

无解响应示例（节选）：

```json
{
  "result": {
    "status": "infeasible",
    "multipliers": [{"index": 0, "value": "1"}],
    "terms": [{
      "index": 0, "b": "-1", "multiplier": "1",
      "weighted_coeffs": ["0"], "weighted_rhs": "-1"
    }],
    "combined_lhs": ["0"],
    "combined_rhs": "-1",
    "combined_relation": "0 <= -1"
  }
}
```

### 最小上界放宽修复

从无解详情（页面按钮或直接调用）发起，请求体只含两个标识：

```json
{"repair_id": "trap-run-2026-09-28-A-fix-1", "audit_id": "trap-run-2026-09-28-A"}
```

* 首次提交（来源存在且结论为无解）→ `201`，修复独立冻结落盘
  （`repair-<sha256>.json`，与来源文件互不影响）；
* 相同修复标识 + **相同来源**（编号与载荷指纹均一致）重传 → `200`，
  返回同一条冻结修复（含相同 `created_at`）；
* 相同修复标识 + **改换来源** → `409 repair_id_conflict`，
  给出已绑定来源编号与指纹，已冻结修复与双方原审计均保持不变；
* 来源不存在 → `404 source_not_found`；来源结论并非无解 →
  `409 source_not_infeasible`；非法请求体 → `400`；以上拒绝均不写盘。

修复响应包含只读的 `source` 快照（来源编号/指纹/规范变量/约束顺序/
稳定标识/原矛盾式证据）与 `result`：

```json
{
  "kind": "min-rhs-relaxation",
  "repair_id": "...",
  "source": {"audit_id": "...", "fingerprint": "sha256:...",
             "variables": ["I1", "I2"],
             "constraint_order": [{"index": 0, "coeffs": [1, 1],
                                   "b": 0, "stable": true}],
             "original_evidence": {"status": "infeasible",
                                   "combined_relation": "0 <= -1"}},
  "result": {
    "status": "relaxed",
    "lex_order": [{"index": 0, "stable": true}],
    "total_relaxation": "4/3",
    "currents": {"I1": "2/3", "I2": "-2/3"},
    "items": [{"index": 0, "b": "0", "increment": "0",
               "new_b": "0", "residual": "0", "stable": true}],
    "dual": {
      "bounds": "[0, 1]",
      "multipliers": [{"index": 0, "value": "1"}],
      "terms": [{"index": 0, "b": "0", "multiplier": "1",
                 "weighted_coeffs": ["1", "1"], "weighted_rhs": "0"}],
      "weighted_lhs": ["0", "0"],
      "neg_weighted_rhs": "4/3",
      "total_relaxation": "4/3",
      "relation": "-sum(lambda_i * b_i) = 4/3 = total_relaxation"
    }
  }
}
```

页面在无解详情中提供“发起最小上界放宽审计”入口（修复标识默认预填
`<audit_id>-fix-1`），修复面板展示电流、逐约束 `d_i / b_i+d_i / 新余量`
与可逐项核验的对偶表；也可在第 4 区按修复编号重读。

## 实现说明

* `app/simplex.py` — 有理数 Phase-I 单纯形：
  自由变量统一拆成 `u − v`；右端为负的行先整体乘 `-1`（记 `σ = sign(b)`，
  对应松弛/剩余变量系数同为 `σ`），加入人工变量得到对任意整数右端都合法的初始基；
  最小化人工变量之和，按 **Bland 固定枢轴规则**（编号最小的负约化成本列入基，
  最小比值、并列时基变量编号最小者出基；人工列永不重新入基）防止退化循环。
  终表目标行松弛/剩余列的约化成本即 Farkas 乘子，代码对 `μ ≥ 0`、
  `μᵀA = 0`、`μᵀb = −w* < 0` 做运行时断言。
* `app/relax.py` — 最小上界放宽 LP（同一自由变量拆分/σ 翻号/Bland 约定）：
  每行加入放宽列 `−σ_i d_i`，两阶段单纯形最小化 `Σ d_i`；首张最优表
  松弛列约化成本恰为有界对偶乘子 `λ_i = r_{s_i} ∈ [0,1]`（放宽列给出
  `r_{d_i} = 1 − λ_i`），并断言 `Aᵀλ = 0`、`−λᵀb = Σd`。随后快照乘子，
  以“加入恒为零的规范等式行 + 恒零人工列”热启动，依次固定 `Σd = T`
  与已优化的稳定坐标，按稳定约束标识序做字典序最小化。
* `app/models.py` — 整数级输入校验（拒绝浮点/布尔/字符串数字、超大整数）。
* `app/storage.py` — 冻结存储：每编号一个 JSON，临时文件 + fsync + 原子 rename，
  进程内锁串行化“检查-写入”，载荷指纹为规范 JSON 的 SHA-256。审计与修复
  分文件前缀（`repair-`）存放，修复绑定键为 `{audit_id, 来源指纹}`。
* `app/web.py` — 仅依赖 Python 标准库的 HTTP 服务；`app/static/index.html`
  为浏览器页面（整数以文本正则校验后直接拼装 JSON，避免 JS `number` 丢精度）。
* `tests/` — 135 个通过的测试；`tests/fourier_motzkin.py` 用与单纯形完全不同的
  Fourier–Motzkin 消元对大量随机/枚举小系统做交叉判定并独立核验证书；
  `tests/test_relax.py` 还以 FM 逐坐标读出精确最小值，独立复核稳定序字典序与
  总放宽量；`scripts/verify.py` 的单次验收经真实 HTTP 重算放宽量与对偶等式。

## 本地（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
AUDIT_PORT=8091 AUDIT_DATA_DIR=./data .venv/bin/python -m app.web
WEB_URL=http://127.0.0.1:8091 .venv/bin/python scripts/verify.py
```
