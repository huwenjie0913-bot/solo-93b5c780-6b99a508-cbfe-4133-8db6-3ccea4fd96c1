"""影响系数法现场动平衡核心：单 / 双平面试重求解、连续配重与孔位离散配平。

基本模型（所有 1X 振动一律按极坐标 amplitude∠phase 换算为复数向量）：

    试重后响应  V1 = V0 + A · T
    影响系数    α_ij = (V1_i − V0_i) / T_j          （T_j = m·r·e^{iθ}，单位 g·mm）
    连续配平    V0 + A · U_corr = 0
                → U_corr = −A⁺·V0                   （M=P 精确求逆，M>P 最小二乘伪逆）
    安装质量    m_corr = |U_corr| / r_install，安装角 = arg(U_corr)
    预测残振    R = V0 + A · U_corr

现场只有少数停机试重机会，因此在给结论前做三道拒绝校验：
1. 维度不一致（测点 / 试重轮次 / 平面数对不上）→ 400 DIMENSION_MISMATCH；
2. 试重前后响应变化过小（相对最大初始振动）→ 400 RESPONSE_CHANGE_TOO_SMALL；
3. 影响系数矩阵病态（复 SVD 条件数超阈值）→ 400 ILL_CONDITIONED_MATRIX。
"""
from __future__ import annotations

import numpy as np

from .errors import ApiError

# 响应变化下限：|ΔV| / max|V0|，小于该比例认为试重响应淹没在测量波动里
DEFAULT_MIN_RESPONSE_RATIO = 0.02
# 影响系数矩阵条件数（σmax/σmin）上限，超过即判定病态、拒绝给配重结论
DEFAULT_MAX_CONDITION_NUMBER = 30.0

# 离散搜索参数：束宽越大越接近全局最优，单平面候选数决定双平面组合枚举量 K^P
_DISCRETE_BEAM_WIDTH = 18
_DISCRETE_KEEP = 12
_DEFAULT_MAX_PIECES = 12


def _r6(v: float) -> float:
    return round(float(v), 6)


def normalize_angle_deg(deg: float) -> float:
    """角度统一归一化到 [0, 360)。"""
    return float(deg) % 360.0


def polar_to_complex(amplitude: float, phase_deg: float) -> complex:
    """极坐标（幅值、度）→ 复数，约定 0° 为键相参考方向、角度逆转为正。"""
    theta = np.deg2rad(phase_deg)
    return complex(amplitude * np.cos(theta), amplitude * np.sin(theta))


def complex_vector_dict(z: complex) -> dict:
    """复数 → 可序列化的实部 / 虚部 / 幅值 / 相位（度，0~360）。"""
    z = complex(z)
    amplitude = abs(z)
    phase = normalize_angle_deg(np.rad2deg(np.angle(z))) if amplitude > 0 else 0.0
    return {"real": _r6(z.real), "imag": _r6(z.imag),
            "amplitude": _r6(amplitude), "phase_deg": _r6(phase)}


# ---------- 输入校验 ----------

def _err(code: str, message: str, details: dict) -> ApiError:
    return ApiError(400, code, message, details)


def _validate_dimensions(req: dict) -> tuple[int, int]:
    """校验测点 / 初始振动 / 试重轮次 / 平面数的维度一致性，返回 (平面数 P, 测点数 M)。"""
    planes = req["planes"]
    points = req["measurement_points"]
    initial = req["initial_vibration"]
    trials = req["trial_runs"]
    P, M = len(planes), len(points)

    if len(initial) != M:
        raise _err(
            "DIMENSION_MISMATCH",
            f"初始振动数量 {len(initial)} 与测点数量 {M} 不一致，每个测点必须给出一组初始 1X 幅值/相位",
            {"field": "initial_vibration", "expected": M, "actual": len(initial),
             "measurement_point_count": M},
        )

    if len(trials) != P:
        raise _err(
            "DIMENSION_MISMATCH",
            f"试重轮次数 {len(trials)} 与配平平面数 {P} 不一致：影响系数法要求每个平面各做一轮独立试重",
            {"field": "trial_runs", "expected": P, "actual": len(trials), "plane_count": P},
        )

    seen: set[int] = set()
    for j, run in enumerate(trials):
        pidx = run["plane_index"]
        if pidx < 0 or pidx >= P:
            raise _err(
                "DIMENSION_MISMATCH",
                f"第 {j} 轮试重 plane_index={pidx} 超出平面范围 [0, {P - 1}]",
                {"field": f"trial_runs[{j}].plane_index", "trial_index": j,
                 "plane_index": pidx, "plane_count": P},
            )
        if pidx in seen:
            raise _err(
                "DIMENSION_MISMATCH",
                f"平面 {pidx} 存在多轮试重，且平面 {sorted(set(range(P)) - seen)} 缺少试重；"
                "每个平面必须恰好有一轮试重",
                {"field": f"trial_runs[{j}].plane_index", "trial_index": j,
                 "plane_index": pidx, "duplicate": True},
            )
        seen.add(pidx)
        if len(run["response"]) != M:
            raise _err(
                "DIMENSION_MISMATCH",
                f"第 {j} 轮试重（平面 {pidx}）的响应数量 {len(run['response'])} 与测点数量 {M} 不一致",
                {"field": f"trial_runs[{j}].response", "trial_index": j, "plane_index": pidx,
                 "expected": M, "actual": len(run["response"])},
            )

    constraint_map: dict[int, dict] = {}
    for k, cons in enumerate(req.get("constraints", [])):
        pidx = cons["plane_index"]
        if pidx < 0 or pidx >= P:
            raise _err(
                "DIMENSION_MISMATCH",
                f"第 {k} 组约束 plane_index={pidx} 超出平面范围 [0, {P - 1}]",
                {"field": f"constraints[{k}].plane_index", "constraint_index": k,
                 "plane_index": pidx, "plane_count": P},
            )
        if pidx in constraint_map:
            raise _err(
                "DIMENSION_MISMATCH",
                f"平面 {pidx} 配置了多组约束，每个平面至多一组",
                {"field": f"constraints[{k}].plane_index", "constraint_index": k,
                 "plane_index": pidx, "duplicate": True},
            )
        constraint_map[pidx] = cons
    return P, M


def _resolve_radii(req: dict, P: int) -> dict[int, float]:
    """解析每个平面的试重安装半径：试重记录显式半径优先，否则取平面半径，都缺则拒绝。"""
    radii: dict[int, float] = {}
    for j, run in enumerate(req["trial_runs"]):
        pidx = run["plane_index"]
        radius = run.get("radius_mm")
        if radius is None:
            radius = req["planes"][pidx].get("radius_mm")
        if radius is None or radius <= 0:
            raise _err(
                "MISSING_TRIAL_RADIUS",
                f"平面 {pidx}（第 {j} 轮试重）未提供试重半径：试重记录 radius_mm 与平面 radius_mm "
                "至少需要给出一个，否则无法把试重换算为不平衡量 g·mm",
                {"field": f"trial_runs[{j}].radius_mm", "trial_index": j, "plane_index": pidx},
            )
        radii[pidx] = float(radius)
    return radii


# ---------- 影响系数与求解 ----------

def _build_influence_matrix(
    req: dict, v0: np.ndarray, P: int, M: int, radii: dict[int, float], min_ratio: float,
) -> tuple[np.ndarray, list[dict], list[dict]]:
    """由各面试重响应构建 M×P 复影响系数矩阵，并校验响应变化幅度。

    返回 (影响系数矩阵 A, 试重不平衡量列表, 响应变化明细)。
    """
    scale = float(np.abs(v0).max())
    if scale <= 0.0:
        raise _err(
            "INITIAL_VIBRATION_TOO_SMALL",
            "所有测点初始 1X 幅值均为 0，缺少动平衡基准，无法计算影响系数",
            {"field": "initial_vibration", "max_initial_amplitude": 0.0},
        )

    columns: dict[int, complex] = {}
    trial_unbalances: list[dict] = []
    changes: list[dict] = []
    small_changes: list[dict] = []

    for j, run in enumerate(req["trial_runs"]):
        pidx = run["plane_index"]
        radius = radii[pidx]
        t_vec = polar_to_complex(run["trial_mass_g"] * radius, run["angle_deg"])
        trial_unbalances.append({
            "trial_index": j, "plane_index": pidx,
            "trial_mass_g": _r6(run["trial_mass_g"]), "radius_mm": _r6(radius),
            "angle_deg": _r6(normalize_angle_deg(run["angle_deg"])),
            "unbalance_g_mm": complex_vector_dict(t_vec),
        })

        col = np.zeros(M, dtype=complex)
        v1 = np.array([polar_to_complex(p["amplitude"], p["phase_deg"])
                       for p in run["response"]], dtype=complex)
        for i in range(M):
            dv = v1[i] - v0[i]
            ratio = abs(dv) / scale
            change = {
                "trial_index": j, "plane_index": pidx, "point_index": i,
                "response_before": complex_vector_dict(v0[i]),
                "response_after": complex_vector_dict(v1[i]),
                "delta": complex_vector_dict(dv),
                "change_ratio": _r6(ratio),
            }
            changes.append(change)
            if ratio < min_ratio:
                small_changes.append({
                    "trial_index": j, "plane_index": pidx, "point_index": i,
                    "field": f"trial_runs[{j}].response[{i}]",
                    "change_amplitude": _r6(abs(dv)),
                    "initial_scale_amplitude": _r6(scale),
                    "change_ratio": _r6(ratio),
                    "min_change_ratio": _r6(min_ratio),
                })
            col[i] = dv / t_vec
        columns[pidx] = col

    if small_changes:
        raise _err(
            "RESPONSE_CHANGE_TOO_SMALL",
            f"有 {len(small_changes)} 个测点的试重响应变化小于初始振动的 "
            f"{min_ratio:.0%}，影响系数会被测量噪声主导，本次试重数据不足以支撑配重结论",
            {"min_change_ratio": _r6(min_ratio),
             "initial_scale_amplitude": _r6(scale),
             "insufficient_changes": small_changes},
        )

    A = np.column_stack([columns[p] for p in range(P)])
    return A, trial_unbalances, changes


def _solve_continuous(A: np.ndarray, v0: np.ndarray, cond_limit: float) -> tuple[np.ndarray, dict]:
    """复 SVD 条件数检查 + 伪逆求连续配重，返回 (配平不平衡量 U_corr, SVD 诊断)。"""
    singular = np.linalg.svd(A.astype(complex), compute_uv=False)
    s_max, s_min = float(singular[0]), float(singular[-1])
    condition = float("inf") if s_min <= 1e-15 * max(s_max, 1e-300) else s_max / s_min

    svd_info = {
        "singular_values": [_r6(s) for s in singular],
        "condition_number": None if not np.isfinite(condition) else _r6(condition),
        "condition_number_limit": _r6(cond_limit),
    }
    if not np.isfinite(condition) or condition > cond_limit:
        raise _err(
            "ILL_CONDITIONED_MATRIX",
            f"影响系数矩阵条件数为 {condition:.3g}，超过上限 {cond_limit:g}："
            "各平面的试重响应高度相关（如两平面几乎同相位同大小），无法区分各平面应承担的配重，"
            "请改变试重角度 / 大小重新试重，或增加测点数做最小二乘",
            {**svd_info, "matrix_shape": [A.shape[0], A.shape[1]]},
        )

    u_corr = -np.linalg.pinv(A.astype(complex), rcond=1e-10) @ v0
    return u_corr, svd_info


# ---------- 离散配重（孔位 + 单块规格 + 最大配重） ----------

def _plane_discrete_candidates(
    target: complex,
    install_radius: float,
    unit_mass: float,
    hole_angles: list[float],
    max_mass: float | None,
    max_pieces: int,
) -> list[dict]:
    """束搜索（beam search）求单平面最逼近连续不平衡量 target 的离散孔位配重候选。

    状态为各孔位块数元组；每步向某个孔位加一块标准配重，按 |Σ n_h·m_u·r·e^{iθ_h} − target|
    保留束宽内最优状态。返回按误差升序的去重候选列表。
    """
    holes = [polar_to_complex(install_radius * unit_mass, ang) for ang in hole_angles]
    n_cap = int(max_mass // unit_mass) if max_mass is not None else max_pieces
    n_cap = max(0, min(n_cap, max_pieces))

    # state: (各孔块数元组, 合成不平衡量, 总质量, 总块数)
    zero = (0,) * len(holes)
    beam: list[tuple] = [(zero, 0j, 0.0, 0)]
    best: dict[tuple, tuple] = {zero: (0j, 0.0, 0)}
    for _ in range(n_cap):
        expanded: dict[tuple, tuple] = {}
        for counts, resultant, total_mass, pieces in beam:
            for h, col in enumerate(holes):
                if counts[h] >= n_cap:
                    continue
                new_counts = counts[:h] + (counts[h] + 1,) + counts[h + 1:]
                if new_counts in expanded or new_counts in best:
                    continue
                new_result = resultant + col
                expanded[new_counts] = (new_result, total_mass + unit_mass, pieces + 1)
        if not expanded:
            break
        ranked = sorted(expanded.items(), key=lambda kv: abs(kv[1][0] - target))
        beam = [(k, *v) for k, v in ranked[:_DISCRETE_BEAM_WIDTH]]
        for k, v in expanded.items():
            best[k] = v

    candidates = [
        {"counts": k, "resultant": v[0], "total_mass": v[1], "pieces": v[2]}
        for k, v in best.items()
    ]
    candidates.sort(key=lambda c: (abs(c["resultant"] - target), c["pieces"], c["total_mass"]))
    return candidates[:_DISCRETE_KEEP]


def _build_discrete_solution(
    req: dict,
    P: int,
    points: list[dict],
    A: np.ndarray,
    v0: np.ndarray,
    u_cont: np.ndarray,
    cont_residual: np.ndarray,
    radii: dict[int, float],
    target: float | None,
) -> dict:
    """在各平面孔位 / 单块规格 / 最大配重约束下枚举离散组合，取预测残振最小者。"""
    constraints = {c["plane_index"]: c for c in req.get("constraints", [])}
    plane_sections: list[dict] = []
    plane_candidates: list[list[dict]] = []
    available = True
    unavailable: list[dict] = []

    for p in range(P):
        cons = constraints.get(p, {})
        plane_radius = radii[p]
        install_radius = cons.get("install_radius_mm") or plane_radius
        holes = cons.get("hole_angles_deg")
        unit_mass = cons.get("unit_weight_mass_g")
        max_mass = cons.get("max_correction_mass_g")
        max_pieces = cons.get("max_pieces_total") or _DEFAULT_MAX_PIECES

        missing = [name for name, value in (
            ("hole_angles_deg", holes), ("unit_weight_mass_g", unit_mass)) if not value]
        section = {
            "plane_index": p,
            "name": req["planes"][p].get("name") or f"平面{p + 1}",
            "install_radius_mm": _r6(install_radius),
            "max_correction_mass_g": _r6(max_mass) if max_mass is not None else None,
            "hole_angles_deg": [_r6(normalize_angle_deg(a)) for a in (holes or [])],
            "unit_weight_mass_g": _r6(unit_mass) if unit_mass is not None else None,
            "max_pieces_total": int(max_pieces),
            "status": "available",
        }
        if missing:
            available = False
            section["status"] = "unavailable"
            section["missing_constraints"] = missing
            unavailable.append({"plane_index": p, "missing_constraints": missing})
            plane_sections.append(section)
            plane_candidates.append([])
            continue

        candidates = _plane_discrete_candidates(
            u_cont[p], float(install_radius), float(unit_mass),
            [float(a) for a in holes],
            float(max_mass) if max_mass is not None else None,
            int(max_pieces),
        )
        # 候选转换为合成不平衡量（g·mm 复数）
        for c in candidates:
            c["plane_index"] = p
            c["hole_angles"] = [float(a) for a in holes]
            c["unit_mass"] = float(unit_mass)
            c["install_radius"] = float(install_radius)
            c["max_mass"] = max_mass
        plane_candidates.append(candidates)
        plane_sections.append(section)

    cont_max = float(np.abs(cont_residual).max())
    over_mass_planes = []
    for p in range(P):
        cons = constraints.get(p, {})
        install_radius = cons.get("install_radius_mm") or radii[p]
        mass = abs(u_cont[p]) / float(install_radius)
        if cons.get("max_correction_mass_g") is not None and mass > float(cons["max_correction_mass_g"]):
            over_mass_planes.append({
                "plane_index": p,
                "continuous_mass_g": _r6(mass),
                "max_correction_mass_g": _r6(cons["max_correction_mass_g"]),
            })

    if not available:
        return {
            "status": "unavailable",
            "target_residual_amplitude": _r6(target) if target is not None else None,
            "target_achieved": None,
            "predicted_residual": None,
            "max_residual_amplitude": None,
            "planes": plane_sections,
            "combinations_evaluated": 0,
            "reasons": ["DISCRETE_UNAVAILABLE"],
            "reason_details": {"planes_missing_constraints": unavailable},
        }

    # 枚举各平面候选的笛卡尔组合（候选数 ≤12，双平面最多 144 组），取全测点残振最大值最小
    best_combo: dict | None = None
    best_score: float | None = None
    evaluated = 0

    def evaluate(p: int, chosen: list[dict]) -> None:
        nonlocal best_combo, best_score, evaluated
        if p == P:
            evaluated += 1
            u_disc = np.array([c["resultant"] for c in chosen], dtype=complex)
            residual = v0 + A @ u_disc
            score = float(np.abs(residual).max())
            total_pieces = sum(c["pieces"] for c in chosen)
            total_mass = sum(c["total_mass"] for c in chosen)
            if best_score is None or (score, total_pieces, total_mass) < (
                    best_score, best_combo["total_pieces"], best_combo["total_mass"]):
                best_score = score
                best_combo = {"chosen": list(chosen), "residual": residual,
                              "total_pieces": total_pieces, "total_mass": total_mass}
            return
        for cand in plane_candidates[p]:
            chosen.append(cand)
            evaluate(p + 1, chosen)
            chosen.pop()

    evaluate(0, [])

    residual_points = []
    for i, pt in enumerate(points):
        r = best_combo["residual"][i]
        within = None if target is None else bool(abs(r) <= target * (1 + 1e-9) + 1e-12)
        residual_points.append({
            "point_index": i,
            "name": pt.get("name") or f"测点{i + 1}",
            **complex_vector_dict(r),
            "within_target": within,
        })

    for p, cand in enumerate(best_combo["chosen"]):
        sec = plane_sections[p]
        pieces = []
        for h, count in enumerate(cand["counts"]):
            if count > 0:
                pieces.append({
                    "hole_angle_deg": _r6(normalize_angle_deg(cand["hole_angles"][h])),
                    "count": int(count),
                    "unit_weight_mass_g": _r6(cand["unit_mass"]),
                    "mass_g": _r6(count * cand["unit_mass"]),
                })
        pieces.sort(key=lambda x: x["hole_angle_deg"])
        sec.update({
            "pieces": pieces,
            "total_pieces": int(cand["pieces"]),
            "total_mass_g": _r6(cand["total_mass"]),
            "resultant_unbalance_g_mm": complex_vector_dict(cand["resultant"]),
            "approximation_error_g_mm": _r6(abs(cand["resultant"] - u_cont[p])),
        })

    target_achieved: bool | None = None
    reasons: list[str] = []
    if target is not None:
        target_achieved = bool(best_score <= target * (1 + 1e-9) + 1e-12)
        if cont_max > target * (1 + 1e-9) + 1e-12:
            reasons.append("CONTINUOUS_TARGET_UNREACHABLE")
        if over_mass_planes:
            reasons.append("CONTINUOUS_EXCEEDS_MAX_MASS")
        if not target_achieved and not reasons:
            reasons.append("DISCRETE_GRANULARITY_INSUFFICIENT")

    return {
        "status": "available",
        "target_residual_amplitude": _r6(target) if target is not None else None,
        "target_achieved": target_achieved,
        "predicted_residual": residual_points,
        "max_residual_amplitude": _r6(best_score),
        "total_pieces": int(best_combo["total_pieces"]),
        "total_mass_g": _r6(best_combo["total_mass"]),
        "planes": plane_sections,
        "combinations_evaluated": evaluated,
        "reasons": reasons,
        "reason_details": {
            "continuous_max_residual_amplitude": _r6(cont_max),
            "continuous_exceeds_max_mass_planes": over_mass_planes,
        } if reasons else {},
        "search": {"beam_width": _DISCRETE_BEAM_WIDTH,
                   "candidates_per_plane": _DISCRETE_KEEP,
                   "default_max_pieces": _DEFAULT_MAX_PIECES},
    }


# ---------- 组装结果 ----------

def _method_basis(M: int, P: int, min_ratio: float, cond_limit: float) -> dict:
    return {
        "sign_convention": "相位 0° 为键相参考方向，角度逆转为正；极坐标 z = amplitude·exp(i·phase)",
        "trial_unbalance": "试重不平衡量 T = m·r·exp(iθ)，单位 g·mm（试重半径取记录 radius_mm，缺省取平面 radius_mm）",
        "influence_coefficient": "α_ij = (V1_i − V0_i) / T_j，构成 M×P 复影响系数矩阵 A",
        "balance_equation": "V0 + A·U_correction = 0",
        "continuous_solution": "U_correction = −A⁺·V0（Moore–Penrose 伪逆）",
        "solver": "exact（测点数=平面数时等价复矩阵求逆）" if M == P else
                  f"least_squares（{M} 测点 / {P} 平面超定，最小化全测点残振平方和）",
        "correction_mass": "安装质量 m = |U_correction| / 安装半径，安装角 = arg(U_correction) 归一化到 [0,360)",
        "predicted_residual": "R = V0 + A·U_correction；目标残振按所有测点预测残振幅值的最大值判定",
        "response_change_gate": f"|ΔV| / max|V0| ≥ {min_ratio:g}，否则拒绝计算",
        "condition_number": f"复矩阵 SVD：cond(A)=σmax/σmin，上限 {cond_limit:g}，超过即判病态拒绝",
        "discrete_search": "各平面在孔位上以单块标准配重做束搜索逼近连续不平衡量，"
                           f"束宽 {_DISCRETE_BEAM_WIDTH}、每平面保留 {_DISCRETE_KEEP} 个候选，"
                           "再枚举平面间组合（双平面最多 144 组），取全测点最大预测残振最小者",
    }


def solve_balancing(req: dict) -> dict:
    """执行完整影响系数动平衡计算，返回可直接持久化 / 序列化的结果字典。

    任何业务校验失败都抛出 ApiError（400），由路由层保证不落库。
    """
    min_ratio = float(req.get("min_response_ratio") or DEFAULT_MIN_RESPONSE_RATIO)
    cond_limit = float(req.get("max_condition_number") or DEFAULT_MAX_CONDITION_NUMBER)

    P, M = _validate_dimensions(req)
    radii = _resolve_radii(req, P)

    points = req["measurement_points"]
    v0 = np.array([polar_to_complex(p["amplitude"], p["phase_deg"])
                   for p in req["initial_vibration"]], dtype=complex)

    A, trial_unbalances, changes = _build_influence_matrix(req, v0, P, M, radii, min_ratio)
    u_cont, svd_info = _solve_continuous(A, v0, cond_limit)
    cont_residual = v0 + A @ u_cont

    constraints = {c["plane_index"]: c for c in req.get("constraints", [])}
    target = req.get("target_residual_amplitude")
    target = float(target) if target is not None else None

    plane_results = []
    for p in range(P):
        cons = constraints.get(p, {})
        install_radius = float(cons.get("install_radius_mm") or radii[p])
        mass = abs(u_cont[p]) / install_radius
        max_mass = cons.get("max_correction_mass_g")
        plane_results.append({
            "plane_index": p,
            "name": req["planes"][p].get("name") or f"平面{p + 1}",
            "install_radius_mm": _r6(install_radius),
            "correction_unbalance_g_mm": complex_vector_dict(u_cont[p]),
            "correction_mass_g": _r6(mass),
            "correction_angle_deg": _r6(normalize_angle_deg(np.angle(u_cont[p], deg=True)))
            if abs(u_cont[p]) > 0 else 0.0,
            "max_correction_mass_g": _r6(max_mass) if max_mass is not None else None,
            "within_max_mass": None if max_mass is None else bool(mass <= float(max_mass) + 1e-12),
        })

    residual_points = []
    for i, pt in enumerate(points):
        r = cont_residual[i]
        residual_points.append({
            "point_index": i,
            "name": pt.get("name") or f"测点{i + 1}",
            **complex_vector_dict(r),
            "within_target": None if target is None else bool(abs(r) <= target * (1 + 1e-9) + 1e-12),
        })
    cont_max = float(np.abs(cont_residual).max())

    continuous = {
        "solver": "exact" if M == P else "least_squares",
        "planes": plane_results,
        "predicted_residual": residual_points,
        "max_residual_amplitude": _r6(cont_max),
        "target_residual_amplitude": _r6(target) if target is not None else None,
        "target_achieved": None if target is None else bool(cont_max <= target * (1 + 1e-9) + 1e-12),
    }

    discrete = _build_discrete_solution(
        req, P, points, A, v0, u_cont, cont_residual, radii, target,
    )

    diagnostics = {
        "measurement_point_count": M,
        "plane_count": P,
        "initial_vectors": [
            {"point_index": i,
             "name": points[i].get("name") or f"测点{i + 1}",
             **complex_vector_dict(v0[i])}
            for i in range(M)
        ],
        "trial_unbalances": trial_unbalances,
        "response_changes": changes,
        "influence_matrix": {
            "shape": [M, P],
            "rows": [
                {"point_index": i,
                 "coefficients": [
                     {"plane_index": p, **complex_vector_dict(A[i, p])} for p in range(P)]}
                for i in range(M)
            ],
        },
        **svd_info,
        "min_response_ratio": _r6(min_ratio),
        "checks": [
            {"check": "dimension_consistency", "passed": True},
            {"check": "response_change", "passed": True,
             "criterion": f"所有 |ΔV| / max|V0| ≥ {min_ratio:g}"},
            {"check": "matrix_condition", "passed": True,
             "criterion": f"cond(A) ≤ {cond_limit:g}", "condition_number": svd_info["condition_number"]},
        ],
    }

    return {
        "equipment_id": req["equipment_id"],
        "speed_rpm": req.get("speed_rpm"),
        "plane_count": P,
        "point_count": M,
        "planes": [
            {"plane_index": p, "name": req["planes"][p].get("name") or f"平面{p + 1}",
             "trial_radius_mm": _r6(radii[p])}
            for p in range(P)
        ],
        "measurement_points": [
            {"point_index": i, "name": points[i].get("name") or f"测点{i + 1}"}
            for i in range(M)
        ],
        "raw_input": req,
        "continuous_solution": continuous,
        "discrete_solution": discrete,
        "diagnostics": diagnostics,
        "method": _method_basis(M, P, min_ratio, cond_limit),
        "note": req.get("note", ""),
    }
