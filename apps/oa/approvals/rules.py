from pathlib import Path
import re
import shutil

from apps.oa.runtime.paths import PROJECT_ROOT


PROJECT = PROJECT_ROOT
RULE_NAMES = {
    "travel_expense":"travel-expense", "business_entertainment":"business-entertainment",
    "business_support_fee":"business-support-fee", "small_marketing_fee":"small-marketing-fee",
    "tpm_small_marketing_fee":"tpm-small-marketing-fee", "general_expense":"general-expense",
    "packaging_layout_approval":"packaging-layout-approval",
}


def freeze_rules(workflow_key, directory):
    reference = Path(".agents/skills/oa-finance-preaudit/references")
    if workflow_key == "overseas_invoice":
        entry = Path(".agents/skills/oa-overseas-invoice/SKILL.md")
        selected = [entry, Path("docs/overseas-invoice.md")]
    else:
        if workflow_key not in RULE_NAMES:
            raise ValueError("rule_workflow_unknown")
        entry = reference / "flows" / (RULE_NAMES[workflow_key]+".md")
        selected = [entry, *(reference / name for name in (
            "result-contract.md", "observation-contract.md", "execution-contract.md"))]
        if workflow_key == "packaging_layout_approval":
            audit = Path(".agents/skills/audit-chinese-image-text")
            selected += [audit / "SKILL.md", audit / "scripts/problem_set.py"]
            selected += [audit / "references" / name for name in (
                "error-taxonomy.md", "output-contract.md", "problem-set.json", "visual-agent-prompts.md")]
        else:
            selected += [reference / name for name in ("payment-screenshot-exemption.md",
                "finance-result-contract.md", "finance-observation-contract.md")]
            selected += [Path("docs/agents") / name for name in (
                "attachment-recognition.md", "image-compression.md", "invoice-originals.md")]
    for relative in selected:
        source = PROJECT / relative
        if not source.is_file():
            raise ValueError("rule_dependency_missing")
    sources = {(PROJECT / relative).resolve() for relative in selected}
    for source in sources:
        if source.suffix != ".md":
            continue
        for link in re.findall(r"\]\(([^)]+)\)",source.read_text(encoding="utf-8")):
            path = link.split("#",1)[0]
            if path and "://" not in path and (source.parent / path).resolve() not in sources:
                raise ValueError("rule_dependency_out_of_scope")
    for relative in selected:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / relative,target)
    return directory / entry
