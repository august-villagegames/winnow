# Winnow

**Turn an open-ended decision into a focused comparison you can react to.**

Winnow is a remote MCP connector that helps an AI agent research a small,
meaningful set of real options, publish an interactive comparison, and learn
from your reactions before the next round. Start with a normal question—rather
than a spreadsheet, a list of options, or a special format.

> “Use Winnow to help me choose a sofa under $2,000. I need something durable
> for a small apartment.”

The agent researches the options and sources, builds the comparison, and
keeps the decision moving. You react to the page instead of having to explain
every preference up front.

## What Winnow is for

Winnow is useful when there are several plausible answers and a little
structured comparison will make your next choice clearer:

- products, furniture, gear, software, or travel options;
- methods, frameworks, vendors, or approaches; and
- any research-backed decision where you want to react your way toward a
  better short list.

It is **not** for sensitive or private decisions. A Winnow page is temporary
and public to anyone who has its link.

## How it works

1. **Ask naturally.** Tell your agent what you are deciding and any constraints
   you already know.
2. **Get a researched first round.** Winnow presents 4–10 representative
   options with sources, comparison details, and images when they help the
   decision.
3. **React on the page.** Like, dislike, or skip options. You can share the
   link with collaborators who should help guide the comparison.
4. **Narrow the next round.** Your agent researches fresh options based on the
   reactions and continues until the decision is clear.

You do not need to write JSON, prepare starter options, or inspect a local HTML
file. The hosted comparison is the result.

## Connect Winnow

The connector gives an MCP-capable AI host a stable Winnow page that can
continue through multiple rounds.

The public MCP endpoint is:

```text
https://winnow-mcp.onrender.com/mcp
```

### Claude Desktop

1. Open **Customize → Connectors**.
2. Choose **Add custom connector**.
3. Name it **Winnow** and enter the endpoint above.
4. In a new conversation, enable **Winnow** from the connector menu.
5. Ask naturally, for example:

   > Use Winnow to research and compare product-management prioritization
   > frameworks. I want an interactive comparison I can react to.

Claude’s own instructions for adding and using a remote custom connector are
available in its [custom-connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
Adding the connector makes it available; enabling it in the conversation lets
Claude use it for that task.

Connector setup alone does not make a host supported. Hosts should complete the
[conformance harness](remote/docs/host-conformance-harness.md) before they are
presented as a tested Winnow integration.

## Before you share a page

The live experience is deliberately collaborative, with a clear boundary:

- the comparison and the choices committed to it are visible to anyone with
  the link;
- while the agent is actively waiting, a link holder can request the current
  round’s one follow-up round;
- the page expires on its original schedule; and
- a link does not grant access to the agent, its provider account, or any
  private credentials.

Only use Winnow for non-sensitive decisions that you are comfortable sharing
with link holders. For a private decision, use a regular conversation instead.

## What a good request looks like

Winnow works from a topic, not a prewritten list. Add whatever constraints will
make the comparison more useful:

| Instead of | Try |
| --- | --- |
| “Help me choose a camera.” | “Use Winnow to help me choose a travel camera under $1,200. Low-light photos and simple controls matter most.” |
| “Compare project-management tools.” | “Use Winnow to compare project-management tools for a five-person design team. We need strong client visibility and a calm interface.” |
| “Which prioritization framework?” | “Use Winnow to compare prioritization frameworks for a product team deciding on its next-quarter roadmap.” |

The agent—not Winnow’s service—does the research and chooses the initial
options. Winnow’s role is to make that research easy to compare and react to.

## For contributors

[`remote/`](remote/) contains the deployable MCP coordinator for live rolling
comparisons. It validates and coordinates sessions; it does not conduct
research or choose options.

Useful technical references:

- [Remote service guide](remote/README.md)
- [Deployment and operations guide](remote/docs/deployment.md)
- [Host-conformance harness](remote/docs/host-conformance-harness.md)

The remote service guide includes deterministic test and production setup
instructions.

## License

Winnow-authored code and documentation are licensed under Apache-2.0. The
bundled Space Grotesk font and Lucide icons retain their upstream licenses; see
[NOTICE](NOTICE) for attribution.
