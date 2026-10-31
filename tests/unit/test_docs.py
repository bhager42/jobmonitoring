"""User-facing docs must stay in step with the files they describe."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_embeds_the_truenas_yaml_exactly() -> None:
    readme = (ROOT / "README.md").read_text()
    match = re.search(
        r"<!-- truenas-app\.yaml: start[^>]*-->\n```yaml\n(.*?)```\n"
        r"<!-- truenas-app\.yaml: end -->",
        readme,
        re.S,
    )
    assert match, "README.md lost its embedded copy of docs/truenas-app.yaml"
    assert match.group(1) == (ROOT / "docs/truenas-app.yaml").read_text(), (
        "README.md's YAML differs from docs/truenas-app.yaml: copy the file into the README"
    )
