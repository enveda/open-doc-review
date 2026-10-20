"""Command line: ``docreview check | run | process | review``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from docreview.audit import AuditLog
from docreview.config import Config, ConfigError, load_config
from docreview.ecm import adapter_class, load_adapter
from docreview.llm import AIService, AnthropicModelClient, ModelClient
from docreview.models import Event
from docreview.pipeline import Pipeline
from docreview.prompts import PromptLibrary

log = logging.getLogger("docreview")


def build(cfg: Config, model_client: ModelClient | None = None) -> Pipeline:
    adapter = load_adapter(cfg.adapter, cfg.adapter_options)
    ai = AIService(model_client or AnthropicModelClient(cfg.llm), cfg.llm, AuditLog(cfg.audit_path))
    return Pipeline(cfg, adapter, ai, PromptLibrary(cfg.prompt_dirs))


def required_prompts(cfg: Config) -> list[str]:
    names = ["classify", "extract"]
    names += [d.review_prompt or f"review/{d.name}" for d in cfg.doc_types.values() if d.review_enabled]
    return names


def cmd_check(cfg: Config) -> int:
    lib = PromptLibrary(cfg.prompt_dirs)
    missing = [n for n in required_prompts(cfg) if not lib.has(n)]
    for n in missing:
        print(f"missing prompt: {n}.md (searched {', '.join(map(str, cfg.prompt_dirs))})")
    try:
        adapter = load_adapter(cfg.adapter, cfg.adapter_options)
        # Optional adapter hook: connection, permissions and schema checks. Returns a list of problems.
        problems = list(adapter.check(list(cfg.doc_types))) if hasattr(adapter, "check") else []
    except Exception as exc:  # report every problem at once rather than the first
        problems = [f"adapter {cfg.adapter!r} failed to start: {exc}"]
    for problem in problems:
        print(problem)
    if missing or problems:
        return 1
    print(f"OK: {len(cfg.doc_types)} document types, adapter {cfg.adapter!r}, prompts in {cfg.prompt_dirs[0]}")
    return 0


def cmd_templates(cfg: Config, fmt: str, adapter_name: str | None) -> int:
    """Print the metadata setup an ECM needs for this config. No connection is made."""
    from docreview.schema import build_schema, match_fields, render_json

    schema = build_schema(cfg)
    name = adapter_name or cfg.adapter
    options = cfg.adapter_options if name == cfg.adapter else {}
    cls = adapter_class(name)
    if fmt == "json" or not hasattr(cls, "render_schema"):
        if fmt != "json":
            print(f"# adapter {name!r} has no native schema renderer; showing the generic schema")
        print(render_json(schema))
        return 0
    print(cls.render_schema(schema, options, match_fields(cfg)))
    return 0


def _load_cursor(path: Path) -> str | None:
    return json.loads(path.read_text()).get("cursor") if path.exists() else None


def _save_cursor(path: Path, cursor: str | None) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": cursor}))
    tmp.replace(path)


def run_once(pipeline: Pipeline) -> int:
    """Handle every pending event. One document's failure never stops the others.

    Two phases: first every document is classified and its fields extracted, then reviews run.
    A batch that holds both a document and its reference therefore reviews correctly in one poll.
    """
    cfg = pipeline.cfg
    try:
        events, cursor = pipeline.ecm.poll(_load_cursor(cfg.state_path))
    except Exception:
        log.exception("poll failed; trying again next cycle")
        return 1
    failed: set[str] = set()
    for review in (False, True):
        for event in list(events):
            if event.ref.id in failed:
                continue
            try:
                result = pipeline.process(event.ref, review_requested=event.kind == "review_requested", review=review)
                queued = {e.ref.id for e in events}
                for ref in result.waiting:  # a reference arrived for a document already reviewed without it
                    if ref.id not in queued:
                        log.info("%s now has its reference %s; queuing a re-review", ref.name, event.ref.name)
                        events.append(Event("review_requested", ref))
                        queued.add(ref.id)
                if review:
                    outcome = result.review.outcome.value if result.review else result.skipped_reason
                    log.info("%s -> %s: %s", event.ref.name, result.doc_type, outcome)
            except Exception:
                failed.add(event.ref.id)
                log.exception("failed on %s (%s); re-run with: docreview process <id>", event.ref.name, event.ref.id)
    _save_cursor(cfg.state_path, cursor)
    return len(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docreview", description="Advisory AI document review for any ECM.")
    parser.add_argument("-c", "--config", default="docreview.yaml", help="path to the YAML config")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate config, prompts and adapter connection")
    tpl = sub.add_parser("templates", help="print the metadata templates / columns / fields your ECM needs")
    tpl.add_argument("--format", choices=["native", "json"], default="native")
    tpl.add_argument("--adapter", help="render for another adapter than the one in the config")
    run = sub.add_parser("run", help="poll the ECM and process new documents")
    run.add_argument("--once", action="store_true", help="one poll, then exit (for cron)")
    run.add_argument("--interval", type=int, default=60, help="seconds between polls")
    for name, help_text in (
        ("process", "classify, extract and (if new) review one document"),
        ("review", "review one document now, even if this version was reviewed"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("document_id")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        return cmd_check(cfg)
    if args.cmd == "templates":
        return cmd_templates(cfg, args.format, args.adapter)
    pipeline = build(cfg)
    if args.cmd == "run":
        if args.once:
            return 1 if run_once(pipeline) else 0
        while True:
            try:
                run_once(pipeline)
            except Exception:  # never let one bad cycle stop the service
                log.exception("cycle failed")
            time.sleep(args.interval)
    ref = pipeline.ecm.get(args.document_id)
    result = pipeline.process(ref, review_requested=args.cmd == "review", manual=True)
    print(json.dumps(asdict(result), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
