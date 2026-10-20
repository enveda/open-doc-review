from __future__ import annotations

import inspect

import pytest
import yaml

from docreview.cli import cmd_check
from docreview.config import ConfigError, load_config
from docreview.ecm import ECMAdapter, load_adapter
from docreview.ecm.local import LocalFolderAdapter
from docreview.prompts import PromptError, PromptLibrary


def _rewrite(workspace, mutate):
    path = workspace / "docreview.yaml"
    raw = yaml.safe_load(path.read_text())
    mutate(raw)
    path.write_text(yaml.safe_dump(raw))
    return path


def test_example_config_checks_clean(workspace, capsys):
    assert cmd_check(load_config(workspace / "docreview.yaml")) == 0


def test_reference_to_unextracted_field_is_rejected(workspace):
    def bad(raw):
        raw["doc_types"]["invoice"]["review"]["references"][0]["match"] = {"po_number": "{order_ref}"}

    with pytest.raises(ConfigError, match="order_ref"):
        load_config(_rewrite(workspace, bad))


def test_reference_to_unknown_type_is_rejected(workspace):
    def bad(raw):
        raw["doc_types"]["invoice"]["review"]["references"][0]["doc_type"] = "contract"

    with pytest.raises(ConfigError, match="contract"):
        load_config(_rewrite(workspace, bad))


def test_missing_review_prompt_fails_check(workspace, capsys):
    (workspace / "prompts" / "review" / "invoice.md").unlink()
    assert cmd_check(load_config(workspace / "docreview.yaml")) == 1
    assert "review/invoice" in capsys.readouterr().out


def test_env_vars_expand(workspace, monkeypatch):
    monkeypatch.setenv("DOCS_ROOT", "library")
    path = _rewrite(workspace, lambda raw: raw["ecm"]["options"].update(root="${DOCS_ROOT}"))
    assert load_config(path).adapter_options["root"] == "library"


def test_prompt_layering_and_front_matter(tmp_path):
    team, shared = tmp_path / "team", tmp_path / "shared"
    for d in (team, shared):
        d.mkdir()
    (shared / "classify.md").write_text("shared {{ x }}")
    (team / "classify.md").write_text("---\nowner: team\n---\nteam {{ x }}")
    p = PromptLibrary([team, shared]).get("classify")
    assert p.render(x=1) == "team 1" and p.meta == {"owner": "team"}
    with pytest.raises(PromptError, match="x"):
        PromptLibrary([shared]).get("classify").render(typo=1)


def test_prompt_cannot_escape_its_folder(tmp_path):
    (tmp_path / "secret.md").write_text("x")
    (tmp_path / "prompts").mkdir()
    with pytest.raises(PromptError):
        PromptLibrary([tmp_path / "prompts"]).get("../secret")


def test_local_adapter_satisfies_port_and_cannot_delete(workspace):
    adapter = load_adapter("local", {"root": "library", "_base_dir": str(workspace)})
    assert isinstance(adapter, ECMAdapter)
    port_methods = {n for n, _ in inspect.getmembers(ECMAdapter, inspect.isfunction)}
    assert not port_methods & {"delete", "move", "rename", "upload", "overwrite"}


def test_local_find_is_normalised_and_scoped(workspace):
    a = LocalFolderAdapter(root="library", _base_dir=str(workspace))
    po = a.get("purchase_orders/PO-7730.txt")
    a.write_metadata(po, "classification", {"doc_type": "purchase_order"})
    a.write_metadata(po, "fields", {"po_number": " po-7730"})
    assert [r.id for r in a.find("purchase_order", {"po_number": "PO-7730"})] == [po.id]
    assert a.find("purchase_order", {"po_number": "PO-7730"}, scope="inbox") == []
    assert a.find("invoice", {"po_number": "PO-7730"}) == []


def test_local_adapter_rejects_paths_outside_root(workspace):
    a = LocalFolderAdapter(root="library", _base_dir=str(workspace))
    with pytest.raises(FileNotFoundError):
        a.get("../docreview.yaml")


def test_root_under_hidden_folder_still_works(tmp_path):
    root = tmp_path / ".hidden" / "lib"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "a.txt").write_text("x")
    (root / ".git").mkdir()
    (root / ".git" / "b.txt").write_text("x")
    a = LocalFolderAdapter(root=str(root))
    assert [e.ref.id for e in a.poll(None)[0]] == ["inbox/a.txt"]


def test_unset_env_var_is_an_error(workspace, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    path = _rewrite(workspace, lambda raw: raw["ecm"]["options"].update(root="${NOT_SET_ANYWHERE}"))
    with pytest.raises(ConfigError, match="NOT_SET_ANYWHERE"):
        load_config(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["doc_types"].update(unknown={"description": "x"}), "reserved"),
        (lambda raw: raw["doc_types"].update(memo="text"), "mapping"),
        (lambda raw: raw["llm"].update(max_tokens="lots"), "invalid value"),
        (lambda raw: raw["llm"].update(fallbacks="Default"), "fallbacks"),
    ],
)
def test_bad_config_is_a_config_error(workspace, mutate, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_rewrite(workspace, mutate))


def test_fallbacks_must_be_off_off_the_claude_api(workspace):
    with pytest.raises(ConfigError, match="Claude API only"):
        load_config(_rewrite(workspace, lambda raw: raw["llm"].update(provider="bedrock")))
    cfg = load_config(_rewrite(workspace, lambda raw: raw["llm"].update(provider="bedrock", fallbacks="off")))
    assert cfg.llm.provider == "bedrock"


def test_every_builtin_adapter_imports_and_has_every_port_method():
    import importlib

    from docreview.ecm import _BUILTIN

    port = {n for n, _ in inspect.getmembers(ECMAdapter, inspect.isfunction) if not n.startswith("_")}
    for name, target in _BUILTIN.items():
        module, _, attr = target.partition(":")
        cls = getattr(importlib.import_module(module), attr)
        assert port <= set(dir(cls)), f"{name} lacks {port - set(dir(cls))}"
        assert not {"delete", "move", "rename", "upload"} & set(dir(cls)), name
