# Learning Journal — example

This is a **format sample**. The real journal Claude writes (`JOURNAL.md`, or
whatever `POP_QUIZ_JOURNAL` points at) is personal and gitignored by default.
Each entry is one pop-quiz: the questions, your answer, the correct answer, a
verdict, and links to study. Newest first.

Verdicts: ✅ correct · 🟡 partial · ❌ missed

---

## 2026-01-15 — Example session: REST caching & database indexing

**Score: 1 ✅ · 1 🟡 · 1 ❌**

### Q1 ✅ HTTP cache headers
- **Asked:** What's the difference between `Cache-Control: no-store` and `no-cache`?
- **My answer:** `no-store` means never save it; `no-cache` means you can store it but must revalidate with the server before reusing.
- **Correct:** Exactly right. `no-store` forbids writing the response to any cache; `no-cache` permits caching but requires revalidation (e.g. via ETag) on every reuse.
- **Study:** [MDN Cache-Control](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control)

### Q2 🟡 Database index trade-offs
- **Asked:** Why not just index every column?
- **My answer:** Indexes make queries faster.
- **Correct:** True but incomplete — the question was the cost. Every index must be updated on each write (slower INSERT/UPDATE/DELETE) and consumes storage, so you index columns you actually filter/join/sort on, not all of them.
- **Study:** [Use The Index, Luke](https://use-the-index-luke.com/)

### Q3 ❌ Connection pooling
- **Asked:** What problem does a database connection pool solve?
- **My answer:** No idea.
- **Correct:** Opening a new DB connection is expensive (TCP + auth handshake). A pool keeps a set of warm connections open and hands them out/reuses them, cutting per-request latency and capping total concurrent connections to the DB.
- **Study:** [Connection pool (Wikipedia)](https://en.wikipedia.org/wiki/Connection_pool)

**Gaps to revisit:** index write-cost trade-offs · connection pooling.
