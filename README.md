# 低温磁阱电流安全审计（coil-audit）

在调整多个线圈电流前，确认安全约束、电源约束与场强线性上界能否**同时**满足。
系统对用户录入的整数不等式 `a·x ≤ b` 给出可复核的精确结论：

* **可行** → 由固定枢轴规则（Bland 规则）得到的一组**有理电流**，以及每条约束的
  **精确余量** `b − a·x`（分数形式，无浮点误差）；
* **无解** → 非负有理 Farkas 乘子，使各约束左侧系数逐项相加为零、右侧和严格为负，
  合并出矛盾式 `0 ≤ -k`，并逐项展示加权合并过程。

工程师读取一条结论为**无解**的冻结审计后，可提交一个**稳定修复标识**
（`repair_id`）发起**最小上界放宽审计**：为每条原约束求非负有理放宽量
`dᵢ ≥ 0`，使 `a·x ≤ b+d` 首次可行，而来源的规范变量、约束顺序、稳定标识
与原证据全部冻结快照、绝不改写。服务以任意精度有理数：

1. **先最小化全部放宽量之和** `t = Σdᵢ`；
2. 再按**稳定约束标识序**（`stable=true` 的约束按原索引升序）字典序最小化
   放宽子向量（非稳定约束只参与总和、不参与平局打破），结果完全确定；
3. 返回对应有理电流、逐约束新右端与新余量，以及证明最优下界的**有界对偶
   乘子** `μ`，逐项满足 `0 ≤ μᵢ ≤ 1`、`Σμᵢ·aᵢ = 0`，且
   `−Σμᵢ·bᵢ = Σdᵢ*`（弱对偶等式即总量最优证明）。

核心算法全部基于**任意精度有理数单纯形**（Python
`fractions.Fraction`，Bland 固定枢轴规则）：原审计为 Phase-I 单纯形，放宽审计
为两阶段 + 字典序单纯形。不使用浮点、采样或外部线性规划服务，也不会在无解时
只报告“无解”而不给证据；来源不存在、并非无解或同一修复标识改换来源时一律
拒绝，原审计及已冻结修复均保持不变。

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
5. 对两个无解矛盾组经真实 HTTP 发起**最小上界放宽审计**（`x≤0` 与
   `-2x≤-1` → `t*=1/2, d=(1/2,0), μ=(1,1/2)`；`x≤0` 与 `-x≤-1` →
   `t*=1, d=(1,0)`），独立重算 `d≥0`、放宽后新余量非负、`0≤μ≤1`、
   `Σμ·a = 0`、`−Σμ·b = Σd`，并核对稳定序字典序、幂等重放（200）、
   按编号重读，以及来源缺失（404）、并非无解（409）、改换修复来源（409，
   已冻结修复不变）。

任一步失败即以非零码退出；全部通过输出 `VERIFY OK` 并以 **0** 退出。
镜像构建本身由 `make verify` 的 `docker compose build` 完成，失败同样中止。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计（JSON 见下） |
| `GET`  | `/api/audits/{audit_id}` | 按编号重新读取冻结结果 |
| `POST` | `/api/repairs` | 对已冻结的“无解”审计发起最小上界放宽审计 |
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

### 最小上界放宽审计

页面从**无解详情**中直接进入（只需填写 `repair_id`），也可用 API：

```json
POST /api/repairs
{"repair_id": "trap-run-2026-09-28-A-fix1", "source_audit_id": "trap-run-2026-09-28-A"}
```

请求只携带两个标识：来源的变量、约束顺序、稳定标识与原证据全部取自冻结
记录，调用方无法借修复通道改写来源。

* 首次提交 → `201`，修复与**来源快照**（含原无解证据）一起冻结落盘；
* 相同修复标识 + 相同来源重传 → `200`，返回同一条冻结修复；
* 来源不存在 → `404 source_not_found`；来源并非无解 →
  `409 source_not_infeasible`；同一修复标识改换来源（含换成不存在的编号）
  → `409 repair_id_conflict`，响应给出已绑定来源与请求来源，**已冻结修复
  与原审计均不变**；非法请求 → `400`。

放宽结果示例（节选；所有数字为精确有理字符串）：

```json
{
  "repair_id": "...-fix1",
  "source_audit_id": "...",
  "source": {"evidence": {"combined_relation": "0 <= -1", "...": "..."}},
  "result": {
    "status": "repaired",
    "total_relaxation": "1/2",
    "stable_order": [0],
    "currents": {"I1": "1/2"},
    "constraints": [
      {"index": 0, "stable": true,  "b": "0",  "relaxation": "1/2",
       "new_b": "1/2", "new_residual": "0"},
      {"index": 1, "stable": false, "b": "-1", "relaxation": "0",
       "new_b": "-1", "new_residual": "0"}
    ],
    "dual": {
      "bounds": "0 <= multiplier <= 1",
      "multipliers": [{"index": 0, "value": "1"}, {"index": 1, "value": "1/2"}],
      "terms": [
        {"index": 0, "multiplier": "1", "weighted_coeffs": ["1"],
         "weighted_rhs": "0", "neg_weighted_rhs": "0"},
        {"index": 1, "multiplier": "1/2", "weighted_coeffs": ["-1"],
         "weighted_rhs": "-1/2", "neg_weighted_rhs": "1/2"}
      ],
      "lhs_sum": ["0"], "rhs_sum": "-1/2", "neg_rhs_sum": "1/2",
      "objective": "-sum(multiplier_i * b_i) = 1/2"
    }
  }
}
```

## 实现说明

* `app/simplex.py` — 有理数 Phase-I 单纯形：
  自由变量统一拆成 `u − v`；右端为负的行先整体乘 `-1`（记 `σ = sign(b)`，
  对应松弛/剩余变量系数同为 `σ`），加入人工变量得到对任意整数右端都合法的初始基；
  最小化人工变量之和，按 **Bland 固定枢轴规则**（编号最小的负约化成本列入基，
  最小比值、并列时基变量编号最小者出基；人工列永不重新入基）防止退化循环。
  终表目标行松弛/剩余列的约化成本即 Farkas 乘子，代码对 `μ ≥ 0`、
  `μᵀA = 0`、`μᵀb = −w* < 0` 做运行时断言。
* `app/relaxation.py` — 最小上界放宽：等式 `a·(u−v) − d + s = b`
  （同样做 `σ` 行翻转、人工变量 Phase-I）；先以 `t=Σd` 为目标做 Bland
  单纯形，再为每个稳定约束（按原索引序）维护一条独立目标行，入基列必须对
  全部上级行约化成本为 0，从而严格在 `t=t*` 最优面上做字典序最小化。
  终表 `t` 目标行松弛列约化成本即有界对偶乘子，运行时逐项断言
  `0 ≤ μ ≤ 1`（`d` 列约化成本 `1−μ ≥ 0`）、`Aᵀμ = 0`、`−μᵀb = t*`。
* `app/models.py` — 整数级输入校验（拒绝浮点/布尔/字符串数字、超大整数）；
  `verify_certificate` 与 `verify_relaxation_certificate` 只依据记录数据
  独立重算证书，不调用求解器（verify 容器与测试共用）。
* `app/storage.py` — 原审计冻结存储；`app/repair_storage.py` — 修复冻结
  存储（独立 `repairs/` 命名空间）：每编号一个 JSON，临时文件 + fsync +
  原子 rename，进程内锁串行化“检查-写入”，指纹为规范 JSON 的 SHA-256。
  修复记录还内嵌**来源快照**（规范变量、约束顺序、稳定标识与原无解证据）。
* `app/web.py` — 仅依赖 Python 标准库的 HTTP 服务；`app/static/index.html`
  为浏览器页面（整数以文本正则校验后直接拼装 JSON，避免 JS `number` 丢精度；
  无解详情显示放宽入口，并可分别按原审计/修复编号重读）。
* `tests/` — 199 个测试；其中 `tests/fourier_motzkin.py` 用与单纯形完全不同的
  Fourier–Motzkin 消元对大量随机/枚举小系统做交叉判定并独立核验证书，
  放宽测试还在细密有理网格上枚举全部候选，独立验证总和最优与稳定序字典序最优。

## 本地（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
AUDIT_PORT=8091 AUDIT_DATA_DIR=./data .venv/bin/python -m app.web
WEB_URL=http://127.0.0.1:8091 .venv/bin/python scripts/verify.py
```
