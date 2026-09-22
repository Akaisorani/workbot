# Managed WeLink tools (V1.4)

WorkBot does not duplicate the company `welink-cli-tool` skill. CodeAgent uses that skill for exact command syntax, while WorkBot provides a policy/rate/audit gateway around the actual `welink-cli` process.

## Capability split

Reply Policy may use read-only operations:

- IM history/recent conversation/group members;
- person/group search and contact lookup;
- meeting list/info/search/topic/materials;
- OneBox metadata/list/search/download;
- mail autodiscover/folders/list/get/attachment;
- calendar list/get.

Execution Policy plus Action Policy/Approval governs writes:

- `im.send`, `im.write`;
- `email.send`, `email.write`;
- `meeting.write`, `calendar.write`;
- `cloud.write`, `cloud.share`;
- `contact.write`.

Authentication login/logout and CLI configuration mutation are operator/admin operations and are not performed automatically by an Agent.

## Managed gateway

When WorkBot starts CodeAgent on Windows it prepends `scripts/tool-bin` to the Agent subprocess PATH. The existing skill can continue to invoke `welink-cli`, but that name resolves inside the Agent environment to WorkBot's gateway wrapper.

The gateway:

1. classifies the CLI operation;
2. enforces read-only vs execution mode;
3. requires a one-use matching approval token for writes;
4. shares a cross-process CLI gate with WorkBot polling/sending;
5. applies shared rate budgets for IM history and OneBox calls;
6. invokes the real WeLink CLI installed on the office PC.

WorkBot itself does not change the operator's system PATH.

## Approval token

An Agent that wants to send mail should first return:

```text
<WORKBOT_APPROVAL>{"action":"email.send","summary":"Send the prepared status mail","details":{"to":"..."}}</WORKBOT_APPROVAL>
```

After policy approval, WorkBot resumes the same CodeAgent session with a one-use token. The gateway accepts one matching `email.send` CLI invocation and marks the token used. A second send needs a new approval.

This makes the CLI layer fail closed even if an Agent forgets the prompt rule and tries a write directly.
