# data/

Everything in this directory is gitignored except `sample.csv` and this file.

Real question CSVs and the SQLite database (which holds subscriber user IDs)
never get committed. If `git status` ever shows a `.csv` here other than
`sample.csv`, or a `.db` file, stop and fix `.gitignore` before committing —
`python scripts/verify.py` checks for exactly this.

## CSV format

`sample.csv` is the schema of record. Columns, in order:

| Column                | Required | Notes                                              |
| --------------------- | -------- | -------------------------------------------------- |
| `question_id`         | yes      | Stable unique key. Re-seeding updates by this.      |
| `subject`             | yes      | Free text, shown as the question's heading          |
| `stem`                | yes      | The question text                                   |
| `option_a`–`option_d` | yes      | All four required                                   |
| `correct_option`      | yes      | One of `A`, `B`, `C`, `D`                           |
| `explanation`         | no       | Sent with the answer; strongly recommended          |
| `scheduled_date`      | no       | `YYYY-MM-DD`. The day this question goes out.       |

`scheduled_date` is what makes a question the question of the day. One
question per date — the loader rejects a file that puts two on the same day,
because only one of them could ever send. A blank date means the question is
loaded but not in the rotation.

Leave the column blank and let the loader lay rows out on consecutive days:

```bash
python scripts/seed.py data/questions.csv --check              # validate only
python scripts/seed.py data/questions.csv --schedule-from 2026-08-11
```

Validation is all-or-nothing: a single bad row fails the file and writes
nothing, because half a question bank is worse than none.
