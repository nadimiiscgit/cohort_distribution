# data/

Everything in this directory is gitignored except `sample.csv` and this file.

Real question CSVs and the SQLite database (which holds subscriber chat IDs)
never get committed. If `git status` ever shows a `.csv` here other than
`sample.csv`, or a `.db` file, stop and fix `.gitignore` before committing —
`python scripts/verify.py` checks for exactly this.

## CSV format

`sample.csv` is the schema of record. Columns:

| Column           | Required | Notes                                            |
| ---------------- | -------- | ------------------------------------------------ |
| `id`             | yes      | Stable unique key. Re-seeding updates by `id`.   |
| `subject`        | yes      | Free text; matched case-insensitively by the bot |
| `year`           | no       | Integer, blank if not applicable                 |
| `stem`           | yes      | The question text                                |
| `option_a`–`option_d` | yes | All four required                                |
| `correct_option` | yes      | One of `A`, `B`, `C`, `D`                        |
| `explanation`    | no       | Sent with the answer; strongly recommended       |
| `source_tag`     | no       | Where the question came from, for your own audit |

Validate before loading:

```bash
python scripts/seed_questions.py data/questions.csv --check
```
