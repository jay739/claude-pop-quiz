<div align="center">

# 📓 Learning Journal — _example_

**A running revision log of every [claude-pop-quiz](README.md).**

</div>

> [!NOTE]
> This file is a **format sample**. Your real journal (`JOURNAL.md`, or wherever
> `POP_QUIZ_JOURNAL` points) is personal and **gitignored by default** — it never
> gets committed.

**Legend** &nbsp;·&nbsp; ✅ correct &nbsp;·&nbsp; 🟡 partial &nbsp;·&nbsp; ❌ missed &nbsp;·&nbsp; newest entry first

| Date       | Topic                            | Score              |
| :--------- | :------------------------------- | :----------------- |
| 2026-01-15 | REST caching & database indexing | 1 ✅ · 1 🟡 · 1 ❌ |

---

## 📅 2026-01-15 — REST caching & database indexing

`1 ✅ · 1 🟡 · 1 ❌`

### ✅ Q1 · HTTP cache headers

> What's the difference between `Cache-Control: no-store` and `no-cache`?

- **Context:** Set in the `cacheControl()` middleware in `api/cache.js` while wiring response headers for the products endpoint.
- **You said:** `no-store` means never save it; `no-cache` means you can store it but must revalidate with the server before reusing.
- **Answer:** Exactly right. `no-store` forbids writing the response to any cache; `no-cache` permits caching but requires revalidation (e.g. via `ETag`) on every reuse.
- 🔗 [MDN, Cache-Control](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control)

### 🟡 Q2 · Database index trade-offs

> Why not just index every column?

- **Context:** Came up adding the `idx_orders_user_id` migration in `db/migrations/014_orders_index.sql`.
- **You said:** Indexes make queries faster.
- **Answer:** True but incomplete, the question was the _cost_. Every index must be updated on each write (slower `INSERT`/`UPDATE`/`DELETE`) and consumes storage, so you index the columns you actually filter/join/sort on, not all of them.
- 🔗 [Use The Index, Luke](https://use-the-index-luke.com/)

### ❌ Q3 · Connection pooling

> What problem does a database connection pool solve?

- **Context:** Touched when configuring `pool.max` in `db/pool.js` during the connection-leak fix this session.
- **You said:** No idea.
- **Answer:** Opening a new DB connection is expensive (TCP + auth handshake). A pool keeps a set of warm connections open and hands them out / reuses them, cutting per-request latency and capping total concurrent connections to the DB.
- 🔗 [Connection pool (Wikipedia)](https://en.wikipedia.org/wiki/Connection_pool)

> **Gaps to revisit:** index write-cost trade-offs · connection pooling.
