import subprocess
import pandas as pd

result = subprocess.run(
    ["python", "-m", "flake8", "."],
    capture_output=True,
    text=True
)

violations = []

for line in result.stdout.splitlines():
    violations.append({
        "Violation": line
    })

if not violations:
    violations.append({
        "Violation": "No PEP8 violations found"
    })

df = pd.DataFrame(violations)

df.to_excel(
    "pep8_report.xlsx",
    index=False
)

print("PEP8 report generated successfully")
