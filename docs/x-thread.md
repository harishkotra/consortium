# X thread — consortium

Two tweets. Character counts are for the text between the fences, verified with
`.venv/bin/python scripts/counts.py` (280-char limit each).

---

## 1/

```text
Two agents. Two companies. One number.

BuyerCorp needs avg order value by region. VendorCorp has the data.
They negotiate a contract over A2A — propose → counter → accept — then the query runs through MCP tools, where the server, not the LLM, decides what's allowed.
```

## 2/

```text
Ask BuyerCorp's agent for customer_email — the column VendorCorp refused — and the "no" comes back from the MCP server:

SCOPE_DENIED: ['customer_email'] was not granted by contract c-61cc5c

No row read. Built on Microsoft Agent Framework 1.x.
```

---

### Optional closer, if you want a link in the thread

Append to tweet 2, or post as a reply:

```text
Repo: https://github.com/harishkotra/consortium
Built by Harish Kotra · https://harishkotra.me · other builds: https://dailybuild.xyz
```
