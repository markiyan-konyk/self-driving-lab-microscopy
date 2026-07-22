"""Assemble the LangGraph state machine.

    connect -> calibration_gate -> setup_camera -> survey -> decide_scene
                                                      ^            |
                                        act_on_scene  |            | route_scene
                                                      +------------+ (act)
                                                                   | (acquire)
                                                                   v
        report <- (abort / budget) --- acquire -> process -> qc -> analyze -> critique
           ^                              ^                                      |
           |                              | (acquire_more)      route_critique   |
           +----- (converged/abort/ ------+--------------------------------------+
                   budget)                |                                      |
                                     relocate <----------- (move_fov) -----------+
                                          |
                                          v
                                        survey

Routers enforce the hard budgets (iterations, survey moves, wall-clock), and a
recursion_limit is passed at invoke time as a final backstop, so the run always
terminates at ``report``.
"""

from functools import partial

from langgraph.graph import END, StateGraph

from . import nodes
from .context import Context
from .state import AgentState


def build_graph(ctx: Context):
    g = StateGraph(AgentState)

    # deterministic + LLM nodes (ctx bound in)
    for name in ("connect", "calibration_gate", "setup_camera", "survey",
                 "decide_scene", "act_on_scene", "relocate", "acquire",
                 "process", "qc", "analyze", "critique", "report"):
        g.add_node(name, partial(getattr(nodes, name), ctx))

    g.set_entry_point("connect")
    g.add_conditional_edges("connect", partial(nodes.route_after_connect, ctx),
                            {"ok": "calibration_gate", "abort": "report"})
    g.add_conditional_edges("calibration_gate",
                            partial(nodes.route_after_calibration, ctx),
                            {"ok": "setup_camera", "abort": "report"})
    g.add_edge("setup_camera", "survey")
    g.add_edge("survey", "decide_scene")
    g.add_conditional_edges("decide_scene", partial(nodes.route_scene, ctx),
                            {"acquire": "acquire", "act": "act_on_scene",
                             "abort": "report"})
    g.add_edge("act_on_scene", "survey")
    g.add_conditional_edges("acquire", partial(nodes.route_after_acquire, ctx),
                            {"process": "process", "survey": "survey",
                             "report": "report"})
    g.add_edge("process", "qc")
    g.add_edge("qc", "analyze")
    g.add_edge("analyze", "critique")
    g.add_conditional_edges("critique", partial(nodes.route_critique, ctx),
                            {"report": "report", "acquire": "acquire",
                             "relocate": "relocate"})
    g.add_edge("relocate", "survey")
    g.add_edge("report", END)
    return g.compile()


def run_graph(ctx: Context, initial_state: AgentState) -> AgentState:
    graph = build_graph(ctx)
    return graph.invoke(initial_state,
                        config={"recursion_limit": ctx.cfg.recursion_limit})
