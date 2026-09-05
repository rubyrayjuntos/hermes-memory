"""hermes-memory viz API — bind 127.0.0.1:7890.

Public facade: helpers in graph_view; Runtime in graph_runtime;
Handler in graph_http; serve/daemon in graph_server.
"""
from __future__ import annotations

from .graph_runtime import _load_dotenv_files  # noqa: F401
from .graph_server import (  # noqa: F401
    PID_PATH,
    RUNTIME,
    Handler,
    Runtime,
    main,
    pid_is_alive,
    read_pid,
    serve,
    start_daemon,
    stop_daemon,
    wait_ready,
    write_pid,
)
from .graph_view import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_LIMIT,
    DEFAULT_PORT,
    GHOST_CONCEPT_LIMIT,
    GHOST_MAX_K,
    GHOST_MAX_LIMIT,
    LOOPBACK_HOSTS,
    MAX_LIMIT,
    _allowed_cors_origin,
    _as_json,
    _stamp_graph_degree,
    assemble_catalog,
    attach_passport_anchors,
    catalog_where_clause,
    clamp_limit,
    conversation_first_budget,
    find_pane_dir,
    human_turn_title,
    humanize_node,
    match_route,
    pack_search,
    parse_agtype_number,
    parse_vertex,
    parse_vertex_id_param,
    preview_embedding,
    safe_label,
    stringify_id,
    undirected_knn_edges,
    unpack_expand_row,
    validate_bind_host,
)
from .session_kind import (  # noqa: F401
    classify_session_kind,
    is_synthetic_session,
    is_verify_session,
)

__all__ = [
    "Handler",
    "Runtime",
    "RUNTIME",
    "main",
    "serve",
    "stringify_id",
    "classify_session_kind",
]

if __name__ == "__main__":
    raise SystemExit(main())
