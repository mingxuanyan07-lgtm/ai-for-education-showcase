"""Read hash-bound display-only recovered meters without rewriting record.json."""
from copy import deepcopy
import hashlib
import json
import os


def load_observation(root, record_path, record):
    identity={key:record[key] for key in ("question_id","ai_id","run_id")}
    sidecar=root / "docs/runtime-observations" / identity["question_id"] / identity["ai_id"] / (identity["run_id"]+".json")
    if not sidecar.is_file():
        return None,None
    try:
        data=json.loads(sidecar.read_text(encoding="utf-8-sig"))
        if data.get("schema_version")!=1 or data.get("identity")!=identity:
            raise ValueError("恢复文件身份不匹配")
        if hashlib.sha256(record_path.read_bytes()).hexdigest()!=data.get("record_sha256"):
            raise ValueError("恢复文件对应旧record版本，请重新审计")
        folder=record_path.parent.resolve()
        registered={(str(a.get("snapshot")),a.get("sha256")) for a in record.get("artifacts",[]) if isinstance(a,dict)}
        sources=data.get("sources") or []
        if not sources:
            raise ValueError("恢复文件没有原始来源")
        for entry in sources:
            source=(root / entry["project_relative_file"]).resolve(strict=True)
            if not source.is_relative_to(folder):
                raise ValueError("来源超出本次原始记录目录")
            snapshot=source.relative_to(folder).as_posix()
            if (snapshot,entry["sha256"]) not in registered:
                raise ValueError("来源未注册为该记录的不可变归档")
            if hashlib.sha256(source.read_bytes()).hexdigest()!=entry["sha256"]:
                raise ValueError("来源SHA256不符")
        observation=deepcopy(data["runtime_observations"])
        observation["reading_link"]=os.path.relpath(sidecar,folder).replace("\\","/")
        return observation,None
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return None,"归档计量恢复材料未通过校验，未采用其中数值："+str(exc)
