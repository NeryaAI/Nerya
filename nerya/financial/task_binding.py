"""Security identity follows task instructions and code, not an occurrence."""
from dataclasses import asdict
from .contracts import FinancialError,security_revision


def strategy_security_revision(config,sid):
    from ..strategies.workflow_service import source_files
    from ..core import yaml_io
    files,source=source_files(config.paths,sid)
    values={}
    for name,text in files.items():
        if name.endswith((".yml",".yaml")):
            import yaml
            values[name]=yaml.safe_load(text)
        elif name.endswith((".py",".js",".ts",".md")):
            values[name]=text
    return security_revision(values)


def task_security_revision(config,kind,tid):
    if kind in {"strategy_agent", "strategy_script"}:return strategy_security_revision(config,tid)
    if kind=="scheduled_agent":
        from ..triggers.schedule import load_schedules
        entry=next((e for e in load_schedules(config.paths) if e.id==tid),None)
        if not entry:raise FinancialError("task_not_found",404)
        return schedule_security_revision(entry,config)
    raise FinancialError("invalid_task_binding",400)


def schedule_security_revision(entry,config=None):
    control={'enabled','archived','cron','every_seconds','run_at','anchor_at','timezone','starts_at','ends_at','overlap_policy'}
    values={key:value for key,value in asdict(entry).items() if key not in control}
    if config is not None and entry.strategy_id:values['bound_strategy_revision']=strategy_security_revision(config,entry.strategy_id)
    return security_revision(values)
