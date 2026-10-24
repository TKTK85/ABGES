# -*- coding: utf-8 -*-
"""
ABGES 基础设施模块
==================
提供全系统共用的路径解析、配置加载、编码检测、日志、时间戳、校验和、
审计记录与统计快照能力。

设计原则：
  - 零重复逻辑：路径、时间戳、审计等通用能力只在此实现一次，全系统调用。
  - 主动防御：读文件前检查存在性与可读性，缺关键项明确报错而非静默继续。
  - 可追溯：任何外部调用与数据增删改都能落到审计流水。
"""

import os
import io
import re
import sys
import json
import time
import hashlib
import logging
import datetime
from abges_runtime.storage import atomic_json

# ---------------------------------------------------------------
# 本机运行环境约束：所有临时数据写 D 盘，不回退 C 盘
# ---------------------------------------------------------------
PREFERRED_TEMP_DIR = os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()
FALLBACK_TEMP_DIR_NAME = ".workbuddy_temp"

ABGES_VERSION = "1.0.0"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# 交付状态三态，禁止其他取值
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
ALLOWED_STATUS = (STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED)


class AbgesError(Exception):
    """ABGES 业务异常，带错误码便于闸门与报告归类。"""

    def __init__(self, code, message, detail=None):
        super(AbgesError, self).__init__("[%s] %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail or {}


# ===============================================================
# 一、路径与项目结构
# ===============================================================

def get_abges_root():
    """返回 ABGES 项目根目录（本文件位于 <root>/tools/Python/）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def resolve_project_path(*relative_parts):
    """把相对于 ABGES 根目录的路径片段拼为绝对路径。"""
    return os.path.join(get_abges_root(), *relative_parts)


def resolve_temp_dir():
    """
    解析可写临时目录。
    优先系统临时目录；不可用时退回项目内 .workbuddy_temp，
    绝不因缺少 D 盘而中断任务。
    """
    for candidate in (PREFERRED_TEMP_DIR,
                      resolve_project_path(FALLBACK_TEMP_DIR_NAME)):
        try:
            ensure_dir(candidate)
            probe = os.path.join(candidate, ".abges_write_probe")
            with open(probe, "w") as handle:
                handle.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    raise AbgesError("E001", "找不到可写的临时目录",
                     {"tried": [PREFERRED_TEMP_DIR, FALLBACK_TEMP_DIR_NAME]})


def ensure_dir(dir_path):
    """确保目录存在，返回该路径。"""
    if dir_path and not os.path.isdir(dir_path):
        os.makedirs(dir_path)
    return dir_path


def make_timestamp():
    """生成统一格式时间戳，用于输出文件命名。"""
    return datetime.datetime.now().strftime(TIMESTAMP_FORMAT)


def make_run_id(task_name):
    """生成运行标识：<时间戳>_<任务名>。"""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_name))
    return "%s_%s" % (make_timestamp(), safe_name)


def build_output_prefix(output_dir, task, trait, model, timestamp=None):
    """
    构造输出前缀，遵循永不覆盖约定：
      {trait}_{timestamp}     （目录本身已含 task_id，无需重复）
    若同名已存在则追加 _2/_3，绝不覆盖既有结果。
    """
    ensure_dir(output_dir)
    stamp = timestamp or make_timestamp()
    base = "%s_%s" % (trait, stamp)   # 简短命名：trait_stamp
    prefix = os.path.join(output_dir, base)
    serial = 2
    while _prefix_occupied(prefix):
        prefix = os.path.join(output_dir, "%s_%d" % (base, serial))
        serial += 1
    return prefix


def _prefix_occupied(prefix):
    """判断某输出前缀是否已被占用（存在任何同前缀文件）。"""
    directory = os.path.dirname(prefix) or "."
    stem = os.path.basename(prefix)
    if not os.path.isdir(directory):
        return False
    return any(name.startswith(stem) for name in os.listdir(directory))


def exclude_json_artifacts(artifacts, root=None):
    """【2026-10-19】从用户可见产物登记中剔除内部 .json 文件。

    【2026-10-20 统一口径】原实现为本函数自带的「任意 .json 一律剔除」后缀规则，
    与 `is_deliverable_path` 两套规则并存，易口径漂移。现改为委托中央判定函数
    `is_deliverable_path`，使展示层与交付物固化层共用同一套规则：
        · 排除 `_runs`（内部运行与审计轨迹）
        · 排除 reports / input / output 目录下的 .json
    调用方请传入 `root`（通常为本次 run_dir），使相对路径形如 reports/x.json，
    层级方可正确判定；不传 root 时按绝对路径兜底判定（绝对路径含 _runs 将被排除）。

    注意：仅过滤“展示层”，不修改 artifacts 注册表本身；流程闸门对产物存在性/
    非空的校验仍依赖完整注册表，不受此函数影响。
    """
    if not isinstance(artifacts, dict):
        return artifacts
    filtered = {}
    for name, value in artifacts.items():
        path = value.get("path") if isinstance(value, dict) else value
        if isinstance(path, str) and path.strip() and not is_deliverable_path(path, root=root):
            continue
        filtered[name] = value
    return filtered


# ===============================================================
# 二、文件读取与编码
# ===============================================================

# HIBLUP 与国内数据文件实际出现过的编码，按命中概率排序
CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


def detect_encoding(file_path):
    """
    检测文本文件编码。优先用实际会出现的编码逐个试解码，
    统计型检测器容易给出低置信度但可解码的编码导致中文乱码。
    """
    require_readable_file(file_path, "E002")
    with open(file_path, "rb") as handle:
        raw = handle.read(200000)
    for encoding in CANDIDATE_ENCODINGS:
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    try:
        import chardet
        guess = chardet.detect(raw)
        if guess and guess.get("encoding"):
            return guess["encoding"]
    except Exception as exc:
        # 不只 ImportError：本机 venv 的 chardet 是残缺的命名空间包，
        # `import chardet` 成功但没有任何属性，detect 会抛 AttributeError。
        # 编码探测属兜底能力，失败必须留痕但绝不阻断主流程。
        logging.getLogger(__name__).warning(
            "chardet 编码探测不可用（%r），回退 utf-8", exc)
    return "utf-8"


def read_text_with_fallback(file_path):
    """按候选编码逐个尝试读取文本，永不因编码问题崩溃。"""
    require_readable_file(file_path, "E002")
    with open(file_path, "rb") as handle:
        raw = handle.read()
    for encoding in CANDIDATE_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def require_readable_file(file_path, error_code="E002"):
    """主动防御：文件必须存在、是文件、可读，否则明确报错。"""
    if not file_path:
        raise AbgesError(error_code, "未提供文件路径")
    if not os.path.exists(file_path):
        raise AbgesError(error_code, "文件不存在：%s" % file_path,
                         {"path": file_path})
    if not os.path.isfile(file_path):
        raise AbgesError(error_code, "路径不是文件：%s" % file_path,
                         {"path": file_path})
    if os.path.getsize(file_path) == 0:
        raise AbgesError(error_code, "文件为空：%s" % file_path,
                         {"path": file_path})
    return file_path


def compute_checksum(file_path):
    """计算文件 MD5，用于原始数据只读校验。"""
    require_readable_file(file_path, "E003")
    digest = hashlib.md5()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_input_file(file_path, rows=None, cols=None):
    """生成输入文件的追溯描述，供 metadata 记录。"""
    return {
        "path": os.path.abspath(file_path),
        "checksum": compute_checksum(file_path),
        "size_bytes": os.path.getsize(file_path),
        "rows": rows,
        "cols": cols,
    }


def describe_output_file(file_path, file_type=None, desc=None):
    """
    生成输出文件描述。文件必须真实存在，否则视为虚报直接报错，
    以此堵死"未生成却声称已生成"的问题。
    """
    if not os.path.exists(file_path):
        raise AbgesError("E004", "声称生成但实际不存在的文件：%s" % file_path,
                         {"path": file_path})
    return {
        "path": os.path.abspath(file_path),
        "type": file_type or os.path.splitext(file_path)[1].lstrip("."),
        "size_bytes": os.path.getsize(file_path),
        "desc": desc or "",
    }


# ===============================================================
# 三、配置加载
# ===============================================================

_CONFIG_CACHE = {}


def load_yaml_config(relative_path, use_cache=True):
    """
    加载 ABGES 配置或 schema。relative_path 相对项目根目录，
    例如 "configs/models/pblup.yaml"。
    """
    import yaml
    abs_path = relative_path
    if not os.path.isabs(relative_path):
        abs_path = resolve_project_path(*relative_path.replace("\\", "/").split("/"))
    if use_cache and abs_path in _CONFIG_CACHE:
        return _CONFIG_CACHE[abs_path]
    require_readable_file(abs_path, "E005")
    content, _ = read_text_with_fallback(abs_path)
    try:
        data = yaml.safe_load(content)
    except Exception as exc:
        raise AbgesError("E005", "配置解析失败：%s" % abs_path, {"error": str(exc)})
    if data is None:
        raise AbgesError("E005", "配置为空：%s" % abs_path)
    _CONFIG_CACHE[abs_path] = data
    return data


def load_model_config(model_id):
    """按模型标识加载模型配置，模型必须已在 model-registry 中登记。"""
    registry = load_yaml_config("configs/models/model-registry.yaml")
    for entry in registry.get("models", []):
        if entry.get("id") == model_id:
            return load_yaml_config(entry["config"])
    known = [entry.get("id") for entry in registry.get("models", [])]
    raise AbgesError("E006", "未登记的模型：%s" % model_id, {"known_models": known})


def resolve_thread_count(requested=None, cap=16):
    """
    解析线程数。requested 为 None 或 "auto" 时取 min(CPU核数, cap)。
    """
    if requested is None or str(requested).lower() == "auto":
        return max(1, min(os.cpu_count() or 4, cap))
    try:
        value = int(requested)
    except (TypeError, ValueError):
        raise AbgesError("E007", "线程数不是整数：%r" % requested)
    if value < 1:
        raise AbgesError("E007", "线程数必须为正整数：%d" % value)
    return min(value, cap)


# ===============================================================
# 四、日志
# ===============================================================

def setup_logger(log_dir, run_id, level=logging.INFO):
    """
    配置根日志器，同时输出到文件与控制台。
    返回日志文件路径。
    """
    ensure_dir(log_dir)
    log_path = os.path.join(log_dir, "abges_%s.log" % run_id)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    # 【S6】日志走 stderr，不可走 stdout：stdout 专供 result JSON，
    # abges_mcp/service.py 的 _json_from_stdout / json.loads(stdout) 依赖其纯净。
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    return log_path


# ===============================================================
# 五、审计流水
# ===============================================================

class AuditTrail(object):
    """
    审计流水记录器，逐条追加写 JSONL。
    记录工具调用、闸门判定、数据剔除、参数覆盖与重试。
    """

    def __init__(self, log_dir, run_id):
        ensure_dir(log_dir)
        self.run_id = run_id
        self.path = os.path.join(log_dir, "audit_%s.jsonl" % run_id)
        self.entries = []

    def _append(self, record):
        record["run_id"] = self.run_id
        record["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.entries.append(record)
        with io.open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def record_tool_call(self, tool, command, exit_code, duration_seconds,
                         workdir=None, note=None):
        """记录一次外部程序调用，命令行完整保留以便复现。"""
        return self._append({
            "event": "tool_call",
            "tool": tool,
            "command": command if isinstance(command, str) else " ".join(command),
            "exit_code": exit_code,
            "duration_seconds": round(duration_seconds, 3),
            "workdir": workdir,
            "note": note,
        })

    def record_gate(self, gate_id, check_id, verdict, actual=None,
                    threshold=None, message=None, stage=None):
        """
        记录一次闸门判定，无论通过与否都记录。

        stage：发起本次判定的阶段（Agent 编号）。同一闸门可被多个阶段绑定
        （如 G1/G2 由 01、02 各评一次，G5 由 07、08 各评一次），不记录
        stage 会导致同 check_id 的多条记录无法归属，损害可追溯性。
        """
        return self._append({
            "event": "gate",
            "gate_id": gate_id,
            "check_id": check_id,
            "stage": stage,
            "verdict": verdict,
            "actual": actual,
            "threshold": threshold,
            "message": message,
        })

    def record_data_change(self, stage, action, affected_count, reason,
                           detail=None):
        """记录一次数据增删改，必须给出影响行数与原因。"""
        return self._append({
            "event": "data_change",
            "stage": stage,
            "action": action,
            "affected_count": affected_count,
            "reason": reason,
            "detail": detail,
        })

    def record_override(self, item, original, new_value, reason):
        """记录一次参数覆盖，理由必填。"""
        if not reason:
            raise AbgesError("E008", "参数覆盖必须给出理由", {"item": item})
        return self._append({
            "event": "override",
            "item": item,
            "original": original,
            "new_value": new_value,
            "reason": reason,
        })

    def record_retry(self, stage, attempt, trigger, action):
        """记录一次重试，禁止静默重试。"""
        return self._append({
            "event": "retry",
            "stage": stage,
            "attempt": attempt,
            "trigger": trigger,
            "action": action,
        })


# ===============================================================
# 六、统计快照与元数据
# ===============================================================

class StatisticsSnapshot(object):
    """
    处理前后统计快照。任何删改数据的步骤都必须记录前后对照，
    未记录者视为无效（闸门 G2-04 阻断）。
    """

    def __init__(self, stage):
        self.stage = stage
        self.before = {}
        self.after = {}

    def capture_before(self, **metrics):
        self.before.update(metrics)
        return self

    def capture_after(self, **metrics):
        self.after.update(metrics)
        return self

    def compute_delta(self):
        """对同名数值指标计算前后差值。"""
        delta = {}
        for key, before_value in self.before.items():
            after_value = self.after.get(key)
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                delta[key] = after_value - before_value
        return delta

    def to_dict(self):
        return {
            "stage": self.stage,
            "before": self.before,
            "after": self.after,
            "delta": self.compute_delta(),
        }

    def is_complete(self):
        """前后统计均非空才算有效。"""
        return bool(self.before) and bool(self.after)


def write_json(path, payload):
    """写 JSON 文件（UTF-8，保留中文），返回路径。"""
    return atomic_json(path, payload, default=_json_default)


def _json_default(value):
    """兜底序列化：把 numpy/datetime 等类型转为可写格式。"""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return str(value)


def write_run_metadata(output_prefix, payload):
    """
    写运行元数据 *_metadata.json。
    要求包含完整命令行，可原样复制执行复现结果。
    """
    required_keys = ("run_id", "timestamp_start", "timestamp_end",
                     "full_command_line", "input_files", "output_files",
                     "status")
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise AbgesError("E009", "运行元数据缺少必需字段",
                         {"missing": missing})
    payload.setdefault("abges_version", ABGES_VERSION)
    return write_json(output_prefix + "_metadata.json", payload)


def write_statistics(output_prefix, snapshots):
    """写处理前后统计 *_statistics.json。"""
    if isinstance(snapshots, StatisticsSnapshot):
        snapshots = [snapshots]
    payload = {"stages": [snapshot.to_dict() for snapshot in snapshots]}
    return write_json(output_prefix + "_statistics.json", payload)


def validate_status(status):
    """校验交付状态取值，禁止模糊表述。"""
    if status not in ALLOWED_STATUS:
        raise AbgesError("E010", "非法交付状态：%r" % status,
                         {"allowed": list(ALLOWED_STATUS)})
    return status


# ==========================================================================
# 交付物过滤（2026-10-20 统一口径）
# --------------------------------------------------------------------------
# 先生要求「交付物过滤」必须集中在单一判定函数里，按【目录名】与【后缀】统一过滤，
# 避免各处各写一套 ignore 规则导致口径漂移。两处消费者必须复用本函数：
#   1) harness/entrypoint/main.py::_consolidate_deliverables  —— 交付件固化拷贝
#   2) 结果展示（最终打印 / 展示列表）                        —— 仅暴露被允许的路径
# 规则：
#   R1 完全排除 `_runs` 目录（内部运行与审计轨迹，非交付物）
#   R2 所有 *.json 一律不纳入交付物、不展示
#      （QC 报告、执行计划、元数据、统计等均为过程件；用户交付件为 docx/xlsx/png/引擎产物）
#   R3 报告诊断日志只用于内部排错，不纳入交付物
# ==========================================================================
ARCHIVE_DIR_NAME = "_runs"


def is_relative_inside(path, ancestor):
    """判断 path 是否位于 ancestor 之内（用于相对路径的前缀校验）。"""
    try:
        path_rel = os.path.relpath(os.path.abspath(path), os.path.abspath(ancestor))
    except ValueError:  # Windows 跨盘符
        return False
    if path_rel == os.curdir:
        return False
    return not path_rel.startswith(os.pardir + os.sep) and path_rel != os.pardir


def is_deliverable_path(path, root=None):
    """
    统一判定给定路径是否允许作为【交付物 / 展示项】。

    path  路径
    root  判定基准目录。调用方必须按场景传入正确的基准：
            · 固化拷贝：root = 拷贝源根（如 <run>/reports），此时源根自身的目录名
              会计入层级上下文，保证其直属 .json 也被正确命中 R2。
            · 结果展示：root = 本次运行的 run_dir 或固定交付目录 output_dir，
              使相对路径形如 reports/result.json，层级可判（同时也是 R1 的判据基准）。

    返回 True 保留；False 过滤（不拷贝、不展示）。

    ⚠ R1 判据说明：有 root 时按【相对路径】判 _runs（因为源根通常本身就在 _runs 内，
      用绝对路径会误杀全部内容）；无 root 时按【绝对路径】判，用于通用兜底。
    """
    raw = str(path or "").strip()
    if not raw:
        return False
    abs_path = os.path.abspath(raw)
    if root:
        rel_path = os.path.relpath(abs_path, os.path.abspath(root))
        if rel_path.startswith(os.pardir):
            rel_path = os.path.basename(abs_path)
        base_ctx = os.path.basename(os.path.abspath(root))
    else:
        rel_path = os.path.basename(abs_path)
        base_ctx = None

    # R1：完全排除 _runs 目录及其内部全部内容
    scan = rel_path if root else abs_path
    parts = [part for part in scan.replace("\\", "/").split("/") if part not in ("", ".")]
    if ARCHIVE_DIR_NAME in parts:
        return False

    # R2：JSON 均为运行过程数据，不属于对外交付内容。
    _, ext = os.path.splitext(rel_path)
    if ext.lower() == ".json":
        return False
    # R3：报告诊断日志供内部排错使用，不能跟随正式报告复制到交付目录。
    if os.path.basename(rel_path).lower().endswith("_报告诊断.log"):
        return False
    # R4：育种值摘要 Markdown 是临时摘要；正式报告统一以 DOCX/XLSX 交付。
    if os.path.basename(rel_path).lower().endswith("_育种值摘要.md"):
        return False
    return True


def make_deliverable_ignore(root):
    """
    返回一个 shutil.copytree(ignore=...) 兼容的可调用对象，按统一口径过滤。

    用法：shutil.copytree(src, dst, ignore=make_deliverable_ignore(src))
    注意：ignore 回调收到的是【待拷贝目录】与其内的名字列表，需拼成完整路径后判定，
          并以 basename 形式返回被忽略项。
    """
    def _ignore(_dir, names):
        ignored = []
        for name in names:
            if not is_deliverable_path(os.path.join(_dir, name), root=root):
                ignored.append(name)
        return ignored
    return _ignore
