# LinkedIn post — consortium

Under 300 characters, as asked. Counts are for the text between the fences; verify with
`.venv/bin/python scripts/counts.py`.

---

## Primary (repo link, no byline)

```text
I built a two-company agent negotiation. BuyerCorp needs a statistic; VendorCorp holds the data. They negotiate a scope contract over A2A, then the query runs through MCP tools — and the MCP server, not the model, refuses the column never granted.

https://github.com/harishkotra/consortium
```

## Variant (byline links, repo named not linked)

```text
I built consortium: two companies' agents negotiate a data-sharing contract over A2A, then run the query through MCP tools — and the MCP server, not the model, refuses the column that was never granted.

Built by Harish Kotra · https://harishkotra.me
More builds: https://dailybuild.xyz
```

## Variant (hook first, for the "see more" fold)

```text
The refusal didn't come from the LLM. It came from the MCP server.

Two agents — BuyerCorp and VendorCorp — negotiate a data-sharing contract over A2A. When one asks for a column the other never granted, the data plane says no and reads nothing.

https://github.com/harishkotra/consortium
```

---

**Note on the byline.** A 300-character ceiling does not leave room for both the repo link and the
`harishkotra.me` / `dailybuild.xyz` links, so pick one variant above. The full byline block lives in
the README and at the foot of `docs/blog.md`.
