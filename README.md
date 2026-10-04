# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地两次调查（本演示为 **2019 → 2024**）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

---

## 1. 关键规则（对应验收要求）

### 1.1 胸径单位必须显式记录
* 每条测量必须带 `dbh_unit`（`cm`/`mm`/`in`），树高单位 `m`。
* 入库时转换为规范单位（胸径 cm、树高 m），**原始值与单位同时保留**，可审计。
* 合理范围检查拦截单位错误（如把 250 mm 当成 250 cm、树高 950 m）。
* 树干坐标必须落在样地边界内（PostGIS 层有 `ST_Contains` 约束兜底）。

### 1.2 异速生长方程显式记录适用树种
`AGB_kg = a · dbh_cm^b · h_m^c`，方程记录：
* 适用树种列表（多对多）、胸径适用范围、是否需要树高；
* 系数 a/b/c、残差 σ(ln AGB)、文献引用、版本号；
* 超出适用径阶的树会在结果中标记 **extrapolation**。

### 1.3 编号是标签，不是身份
**编号相同但位置矛盾时，先核实，不能直接认成同株。**

* 内部 `tree_id` 才是个体身份；同树行换标签 = 已核实改号（renumber）。
* 同编号、不同树行、位置矛盾 → 生成 `IdentityConflict`（open），
  在人工核实前**从所有分量中剔除**，不会悄悄变成死亡或进界。
* 核实结论只有人工给出：
  * `renumber`：同一株树换了牌号 → 计入存活木生长；
  * `distinct`：不同个体 → t1 计入死亡、t2 计入进界。
* 新编号出现在旧树附近 → “possible renumber” 待核实，绝不自动合并。

### 1.4 真实零生长、缺测、死亡三者严格区分
| 情况 | 字段 | 处理 |
|---|---|---|
| 真实零生长 | `alive_measured`，两次均测，|Δdbh| ≤ 0.15 cm 且有复核记录 | 计入生长量（增量≈0），结果中列出 |
| 缺测 | `alive_not_measured`（活着但胸径未测） | **不是零**；按样地比率插补，方差膨胀，列出清单 |
| 死亡 | `dead`（t2 有死亡观测） | 以 t1 生物量计入死亡量 |
| 未找到 | `missing_tree` | 不入死亡量，列入 provenance |
| 进界以下 | t2 新树 dbh < 5 cm | 记录但不计入进界 |

### 1.5 总体估计按抽样设计加权
分层简单随机抽样，**不把所有树木平均后乘面积**：

```
样地分量 y [kg/ha] = 分量(kg) / 该样地自己的面积(ha)
Y_h = A_h · mean_h(y)                 # A_h = 已知的层土地面积
SE_h = A_h · sqrt( (1−f_h) · s_h²/n )  # 有限总体校正可选
Y = Σ_h Y_h，SE 跨层合成（Welch–Satterthwaite 自由度，t 分布 95% CI）
```

演示数据刻意使用**不等面积样地**（0.20 / 0.50 / 1.00 ha）。

### 1.6 已确认调查版不可被新方程静默改变
* `EstimateVersion`：draft → `confirm` 后结果载荷、设计快照、方程校验和全部冻结。
* 模型层 + PostGIS 触发器双重禁止修改 confirmed 版本。
* 确认时同时**锁定所用方程**（系数不可改）；新系数必须以**新方程 code/version** 录入，
  并产生**新版本估计**，旧版本数字永不改变。

### 1.7 野外平板批次同步（离线、可恢复、幂等）
野外平板无网时把**一次调查（Survey）拆成多批（SurveyBatch）**上传，可能 2024 批先到、
2019 批后到，也可能网络恢复时整批重发：

* **幂等提交**：每批携带客户端 `idempotency_key`（唯一）与内容 SHA-256 摘要。
  相同键 + 相同载荷 → 返回**原回执**，零写入；相同键 + 不同载荷 → 409 拒绝。
* **状态机**：`staged → validated → applied / blocked / failed`。
  所有行通过校验后才在**一个原子事务**里写入测量；事务中断整体回滚，
  批次停留在 `validated`（可重试），不会留下部分测量。
* **先到数据不可被后到批次覆盖**：某批的行与已应用记录（同 campaign/样地/编号）
  内容冲突 → 批次 `blocked`，生成 `BatchConflict` 进入人工处理
  （保留先到 / 接受为更正 / 接受为共享编号的新个体），处理后调用"继续应用"。
  内容完全一致的跨批重发折叠为 `duplicate`，不算冲突。
* **相邻调查齐备才计算派生量**：身份冲突扫描只在两侧 campaign 的调查都
  `complete`（全部批次到齐或人工 finalize）后执行且记录一次（`SurveyScan`）；
  估计接口在相邻数据不齐时返回 409 —— 先到的 2024 批不会提前造出死亡或进界。
* **审计**：`MeasurementImportRow` 在批次**暂存时**即落库（staged→validated→
  applied/rejected/duplicate/conflicted/dropped），应用前后均可经
  `/api/import-rows/` 查询；每批的回执（逐行状态）存于批次上供重发放回。

---

## 2. 不确定性假设（结果中完整输出）
1. **设计推断**：分层 SRS，每样地等权（每公顷基准），层土地面积放大；树木从不合并平均。
2. **测量误差**：胸径 σ=0.10 cm、树高 σ=0.30 m，独立高斯，一阶误差传播；
   作为诊断分量单独报告（不与样地间抽样方差重复计入 SE 合计）。
3. **方程残差**：乘法对数正态 σ(ln AGB)；存活木两次用同一方程、残差假定完全相关故增量抵消；
   死亡（仅 t1）与进界（仅 t2）的方程误差保留。
4. **缺测存活木**：假定样地内 MAR，用“有测样木 t1 生物量生长率”做比率插补，
   抽样方差按 1/(1−缺测生物量比例) 膨胀。
5. 未核实身份、未找到、进界以下个体**排除在分量外**并在 provenance 列明。
6. 95% CI 用跨层 Welch–Satterthwaite df 的 t 分布；净变化 SE 中分量抽样协方差假定为 0。
7. 样地申报面积与多边形面积交叉核对（1% 容差）。

---

## 3. 运行

### 后端
```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate      # 可选
pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo        # 载入虚构数据（含全部验收场景）
python3 manage.py runserver 127.0.0.1:8123
```

PostgreSQL/PostGIS：
```bash
FOREST_DB=postgis PGHOST=.. PGUSER=.. PGPASSWORD=.. \
  python3 manage.py migrate
psql -d foreststation -f ../deploy/postgis.sql
```

### 前端
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, /api 代理到 8123
```

界面四页：
1. **Plots & individuals**：SVG 地图显示全部样地边界与 t2 个体状态；点入样地看 t1→t2 复测、
   改号、零生长/缺测/死亡着色；
2. **Survey uploads**：调查上传进度（survey/batch 状态）、逐行回执、提交新批、
   重叠记录人工处理与"继续应用"；
3. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct）；
4. **Estimates**：选择方程→跑 draft→查看分量、来源、不确定性→确认冻结。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2024` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open` | 同号位置矛盾 |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber|distinct, note}` |
| POST | `/api/imports/` | 批量入库（旧直导路径，207 返回明细） |
| POST | `/api/batches/` | **批次提交**（幂等键；重发放回原回执；重叠→409 blocked） |
| GET | `/api/batches/`、`/api/batches/{id}/` | 上传进度、批次详情与逐行回执 |
| POST | `/api/batches/{id}/apply/` | **继续应用** validated/blocked 批次（可重试） |
| GET | `/api/surveys/` | 各调查批次到齐进度 |
| POST | `/api/surveys/{id}/finalize/` | 宣布调查到齐，触发相邻扫描 |
| GET | `/api/batch-conflicts/?status=open` | **重叠树记录冲突查看** |
| POST | `/api/batch-conflicts/{id}/resolve/` | `{action: keep_existing|accept_incoming|accept_as_new}` |
| GET | `/api/import-rows/?batch=&state=` | 行级审计（应用前后均可查） |
| POST | `/api/estimates/` | 运行 draft 估计（相邻调查不齐→409） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |

### 批次提交示例
```json
{
  "survey": "2024-field",
  "idempotency_key": "tablet-7-2024-P01-v1",
  "campaign": "2024",
  "expected_batches": 6,
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```
响应即回执（同时存库供重发放回）：`201 applied / 202 validated(可重试) /
409 blocked(含冲突明细) / 422 failed(含逐行拒因)`；相同键重传返回 `200 + replay:true`。

### 入库行示例
```json
{
  "campaign": "2024",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
23 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及批次同步验收场景：
* 多批合并 → 一次完整导入 + 恰好一次冲突扫描；
* 相同键+相同载荷重传 → 返回原回执、零重复写入；相同键不同载荷 → 409；
* 重叠树记录 → 批次 blocked、先到数据不被覆盖，人工处理后继续应用；
* 后期批次先到 → 扫描推迟、估计接口 409，不产生死亡/进界；
* 应用事务中断 → 可重试状态、无部分测量，重试无双倍冲突。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 2024 调查经**批次管线**入库：5 个样地批次（`seed-2024-P01..P05`）应用成功，
  `seed-2024-corrections` 批（4 条坏行：mm 当 cm、树高 cm 当 m、缺单位、坐标越界）
  **整批 failed**、逐行拒因可查；调查 finalize 后触发一次身份扫描；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等。
