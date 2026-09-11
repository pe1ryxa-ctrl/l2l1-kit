---
trigger: always_on
---

# Project Rules — {{TITLE}} [{{TAG}}]

> **Ролі, межі воркспейсу, протокол задач і звітів — у `.agents/AGENTS.md`. Тут — лише технічні правила проєкту.**

## Technology Stack
- **Runtime / Language**: …
- **Framework**: …
- **Storage**: …
- **Deploy target**: …
- **Testing**: …

## Project Structure
```text
{{P}}/
├── …
├── Context_{{X}}.md      # ⬅ READ FIRST — архітектура, контракти, відомий техборг
├── Plan_{{X}}.md         # актуальний беклог (лише невиконане)
└── Changelog_{{X}}.md    # історія виконаного
```

## Key Documents — Read Before Working
| Document | Purpose |
|---|---|
| `Context_{{X}}.md` | Повна архітектура, інтерфейси, модель безпеки |
| `Plan_{{X}}.md` | Беклог; перевіряти при відкритті воркспейсу |
| `Changelog_{{X}}.md` | Історія версій і виконаних задач |

## Development Rules
- Використовуй наявні канали обміну між модулями (API, черги, контракти); не ламай логіку інших модулів.
- Секрети — лише в `.env` (є `.env.template`), ніколи в коді, командах чи git.
- Тести — для кожної нової функції/ендпоінта; перед Report — увесь набір зелений.

## Build & Run
```text
…
```

## Documentation Update Policy
- `Context_{{X}}.md` — при нових модулях, інтерфейсах, зміні архітектури.
- `Changelog_{{X}}.md` — при завершенні фічі/фікса (версія + короткий підсумок).
- `Plan_{{X}}.md` — видалити виконане, додати виявлене.
- Цей файл — коли змінюється стек, структура або правила.

## Known Pitfalls
- …
