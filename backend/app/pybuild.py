"""Build Python from existing scripts.

Agents pick up your existing scripts through the registered MCP servers
(mcp.py) and use them as context to generate Python for a new request. The
generated Python is a deliverable — Studio does not execute arbitrary Python;
to RUN it against an environment, submit it through the supervised Jobs path
(supervisor.py), where a human approves before anything runs on prod.

Without an LLM key this degrades to a commented scaffold so the flow is
demoable; with a key, the agent drafts Python and — when MCP script servers
are registered — reads the existing scripts as context first.
"""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db
from .auth import current_user

router = APIRouter(prefix="/python", tags=["python"])

_SYS = """You are a senior data engineer. Using the existing scripts available to you as context (via the connected tools), write ONE Python module that fulfils the user's request in the same style and conventions as those scripts.

Rules:
- Return ONLY Python code — no prose, no markdown fences.
- Reuse helpers, imports, and patterns from the existing scripts where they fit.
- Make it runnable and self-contained; add a short module docstring.
- Do not invent credentials; read config from env or function arguments."""


class BuildIn(BaseModel):
    prompt: str
    context: str | None = None   # optional inline script context


@router.post("/build")
def build(body: BuildIn, user=Depends(current_user)):
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe what the Python should do")

    spec = agent.llm_spec()
    used_mcp = list(agent.mcp_servers().keys())

    if not agent.llm_available(spec, user):
        code = _scaffold(prompt, body.context)
        return {"python": code, "mode": "fallback", "mcp_servers": used_mcp,
                "note": "No LLM key — returned a scaffold. Connect a key for real generation."}

    try:
        code = _generate(prompt, body.context, user, spec, used_mcp)
    except Exception as e:
        code = _scaffold(prompt, body.context)
        return {"python": code, "mode": "fallback", "mcp_servers": used_mcp,
                "note": f"Generation error ({e}); returned a scaffold."}

    db.log_activity(user, "python_build", prompt=prompt[:200])
    return {"python": code, "mode": "agent", "mcp_servers": used_mcp}


def _generate(prompt, context, user, spec, used_mcp):
    """Draft Python, using MCP script servers as context when registered."""
    import re

    messages = [("system", _SYS)]
    if context:
        messages.append(("user", f"Existing script for reference:\n{context[:6000]}"))
    messages.append(("user", f"Build Python for: {prompt}"))

    mcp_cfg = agent.mcp_servers()
    if mcp_cfg:
        # MCP tools (e.g. a filesystem/git server exposing existing scripts) let
        # the agent read them before writing. Run the graph on an event loop.
        import asyncio

        async def _arun():
            tools = list(await agent._load_mcp_tools(mcp_cfg))
            llm = agent.make_llm(spec, user)
            graph = agent._build_graph(llm, tools, _SYS)
            return await graph.ainvoke(
                {"messages": [("user", f"Build Python for: {prompt}"
                                       + (f"\n\nReference script:\n{context[:6000]}" if context else ""))]},
                config={"recursion_limit": 12})

        result = asyncio.run(_arun())
        text = agent._final_text(result)
    else:
        llm = agent.make_llm(spec, user)
        reply = llm.invoke(messages)
        text = reply.content if isinstance(reply.content, str) else "".join(
            b.get("text", "") for b in reply.content if isinstance(b, dict))

    return re.sub(r"^```(python)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()


def _scaffold(prompt, context):
    ref = "\n# Reference script provided:\n# " + "\n# ".join(
        (context or "").splitlines()[:8]) if context else ""
    return (
        f'"""{prompt}\n\nGenerated scaffold — connect an LLM key (and register an\n'
        f'MCP scripts server) for the agent to build this from your existing scripts.\n"""\n'
        "import os\n\n\n"
        "def main():\n"
        f"    # TODO: implement — {prompt}\n"
        "    raise NotImplementedError\n"
        f"{ref}\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )
