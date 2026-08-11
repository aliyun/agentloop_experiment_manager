# Third-party SDK snapshots

`agentloop-go-sdk/` is the official Alibaba Cloud AgentLoop Go Tea SDK:

- Module: `github.com/alibabacloud-go/agentloop-20260520/v2`
- Version: `v2.3.1`
- Tag commit: `1df1ac2d59f89ea0ddd5a4707ea34cba6b49e6c7`
- API version: `2026-05-20`
- Downloaded: `2026-08-07`

The directory is a vendored snapshot of the exact upstream tag. Upstream repository:
<https://github.com/alibabacloud-go/agentloop-20260520>. The local contract test in
`sdk/agentloop-go-contract/` consumes this snapshot through a Go `replace`
directive and makes no AgentLoop API calls.
