import subprocess
import pandas as pd

file_to_check = "backup.py"

result = subprocess.run(
    ["python", "-m", "pylint", file_to_check],
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

report_name = file_to_check.split(".")[0] + "_report.xlsx"

df.to_excel(
    report_name,
    index=False
)

with open("report_name.txt", "w") as f:
    f.write(report_name)
    
print("PEP8 report generated successfully")
