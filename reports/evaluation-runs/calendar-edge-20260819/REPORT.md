# §18.3 H calendar edges

- started_at: `2026-08-19T13:57:00.902710+00:00`
- result: **8/8 PASS**

| ID | Result | Title |
|---|---|---|
| H-01 | PASS | 下下周三 resolves to the Wednesday of the week after next |
| H-02 | PASS | 这周五还是下周五 does not invent a day |
| H-03a | PASS | 8/5 before the day is 2026-08-05 |
| H-03b | PASS | 8.5 before the day is 2026-08-05 |
| H-03c | PASS | 2026.8.5 keeps the named year even if that day is past |
| H-03d | PASS | Yearless 8/5 in the recent past is left unresolved |
| H-04 | PASS | 春节 does not become an invented Gregorian day |
| H-05 | PASS | 12月30日去 1月2日回 wraps the return into the next year |

