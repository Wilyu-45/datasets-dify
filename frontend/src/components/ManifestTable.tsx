import {
  Button,
  Card,
  Input,
  InputNumber,
  message,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import {
  getManifest,
  updateManifestRow,
  type ManifestRow,
  type ParseReport,
  type ParseActionRecord,
  type ChunkReport,
  type ChunkActionRecord,
  type DifyReport,
  type DifyActionRecord,
} from "../api/client";

const STATUS_COLORS: Record<string, string> = {
  pending: "gold",
  done: "green",
  error: "red",
  new: "blue",
  scanning: "cyan",
  parsing: "cyan",
  parsed: "green",
  chunking: "cyan",
  chunked: "green",
  uploading: "cyan",
  parsing_done: "green",
};

const DIFY_STATUS_COLORS: Record<string, string> = {
  done: "green",
  error: "red",
  uploading: "cyan",
  dry_run: "gold",
};

/** 从 filename 提取 stem（去后缀） */
function fileStem(filename: string): string {
  const i = filename.lastIndexOf(".");
  return i > 0 ? filename.slice(0, i) : filename;
}

/**
 * 把 ParseReport.actions 中本次解析/失败/试运行的结果合并到 rows。
 * - 匹配规则：actions[i].filename == rows[j].filename
 * - 若后端已更新过该行，rows 中 parse 已是新值（再合并也是幂等的）
 * - 但如果用户在 load() 之后还没等 GET 完，actions 已能保证 UI 立刻反映本次结果
 */
function applyParseActionsToRows(
  rows: ManifestRow[],
  actions: ParseActionRecord[] | undefined
): ManifestRow[] {
  if (!actions || actions.length === 0) return rows;
  const byName = new Map<string, ParseActionRecord>();
  for (const a of actions) byName.set(a.filename, a);
  return rows.map((r) => {
    const a = byName.get(r.filename);
    if (!a) return r;
    let newParse = r.parse ?? null;
    let newStatus = r.status ?? null;
    if (a.action === "parsed") {
      newParse = a.parse_dir ?? newParse;
      newStatus = "parsed";
    } else if (a.action === "parse_failed") {
      newParse = a.error ? `解析失败 → ${a.error}` : "解析失败";
      newStatus = "error";
    } else if (a.action === "dry_run_parse") {
      newParse = "试运行-已识别";
      newStatus = "pending";
    }
    if (newParse === r.parse && newStatus === r.status) return r;
    return { ...r, parse: newParse, status: newStatus };
  });
}

/**
 * 把 ChunkReport.actions 中本次切分/失败/跳过的结果合并到 rows。
 * - 匹配规则：actions[i].filename == rows[j].filename
 * - action 取值：chunked / skipped_chunked / chunk_failed / no_parsed / dry_run_chunk
 */
function applyChunkActionsToRows(
  rows: ManifestRow[],
  actions: ChunkActionRecord[] | undefined
): ManifestRow[] {
  if (!actions || actions.length === 0) return rows;
  const byName = new Map<string, ChunkActionRecord>();
  for (const a of actions) byName.set(a.filename, a);
  return rows.map((r) => {
    const a = byName.get(r.filename);
    if (!a) return r;
    let newChunks = r.chunks ?? null;
    let newStatus = r.status ?? null;
    if (a.action === "chunked") {
      // chunks 列只存 stem（与后端约定一致）
      const stem = a.chunks_dir ? a.chunks_dir.split(/[\\/]/).pop() : r.chunks;
      newChunks = stem ?? newChunks;
      newStatus = "chunked";
    } else if (a.action === "skipped_chunked") {
      // 跳过：保持原 chunks，但 status 提示
      newStatus = "chunked";
    } else if (a.action === "chunk_failed") {
      newChunks = a.error ? `切分失败 → ${a.error}` : "切分失败";
      newStatus = "error";
    } else if (a.action === "no_parsed") {
      newChunks = a.error || "无解析结果";
      newStatus = "pending";
    } else if (a.action === "dry_run_chunk") {
      newChunks = "试运行-已切分";
      newStatus = "chunking";
    }
    if (newChunks === r.chunks && newStatus === r.status) return r;
    return { ...r, chunks: newChunks, status: newStatus };
  });
}

/**
 * 把 DifyReport.actions 中本次入库结果合并到 rows。
 * - 匹配规则：actions[i].stem == rows[j].filename 的 stem
 * - action 取值：uploaded / skipped_done / failed / dry_run
 */
function applyDifyActionsToRows(
  rows: ManifestRow[],
  actions: DifyActionRecord[] | undefined
): ManifestRow[] {
  if (!actions || actions.length === 0) return rows;
  const byStem = new Map<string, DifyActionRecord>();
  for (const a of actions) byStem.set(a.stem, a);
  return rows.map((r) => {
    const a = byStem.get(fileStem(r.filename));
    if (!a) return r;
    let newDocId = r.dify_doc_id ?? null;
    let newStatus = r.dify_status ?? null;
    if (a.action === "uploaded") {
      newDocId = a.dify_doc_id ?? newDocId;
      newStatus = "done";
    } else if (a.action === "skipped_done") {
      newStatus = "done";
    } else if (a.action === "failed") {
      newStatus = "error";
    } else if (a.action === "dry_run") {
      newStatus = "dry_run";
    }
    if (newDocId === r.dify_doc_id && newStatus === r.dify_status) return r;
    return { ...r, dify_doc_id: newDocId, dify_status: newStatus };
  });
}

export default function ManifestTable({
  refreshKey,
  parseReport,
  chunkReport,
  difyReport,
}: {
  refreshKey: number;
  /** 兼容旧调用方 */
  parseReport?: ParseReport | null;
  /** 兼容旧调用方（同时存在时以 lastReport 传入） */
  chunkReport?: ChunkReport | null;
  /** §3.4 Dify 入库结果 */
  difyReport?: DifyReport | null;
  /** @deprecated use parseReport */
  lastReport?: ParseReport | null;
}) {
  const [rows, setRows] = useState<ManifestRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);

  // 兼容旧 prop 名（ParsePage 还在传 lastReport）
  const effectiveParse = parseReport ?? null;
  const effectiveChunk = chunkReport ?? null;
  const effectiveDify = difyReport ?? null;

  const load = async (p = page) => {
    setLoading(true);
    try {
      const data = await getManifest(50, (p - 1) * 50);
      // 拉取后立即用 parseReport/chunkReport/difyReport.actions 合并本次结果 → UI 立刻反映
      let merged = applyParseActionsToRows(data.rows, effectiveParse?.actions);
      merged = applyChunkActionsToRows(merged, effectiveChunk?.actions);
      merged = applyDifyActionsToRows(merged, effectiveDify?.actions);
      setRows(merged);
      setTotal(data.total);
    } finally {
      setLoading(false);
    }
  };

  // ---- 文档元数据（doc_metadata 表，→ Dify 元数据）----
  // ★ 2026-08-31 已迁移到「文档元数据」页（以 Dify 库内文档清单为准），
  //   本表不再承载元数据编辑 / 导入 Dify 入口。

  // ---- 表格 ----

  // ---- 行内编辑（web 端维护清单元数据，替代原 Excel 填列） ----
  // ★ 2026-08-31 一级分类/二级分类/关键词/适用科室/生效日期/校对等元数据列已移至
  //   「文档元数据」页统一填写（本表只保留序号，不再显示元数据列）
  type EditableField = "seq";

  const [editing, setEditing] = useState<{ filename: string; field: EditableField } | null>(
    null
  );
  const [editingText, setEditingText] = useState("");
  // 记录"上一次已处理的单元格"，仅用于消除 Enter + blur 双触发导致的重复保存；
  // 不同单元格之间的保存互不阻塞（避免一个保存未完成时其他单元格无法编辑/保存）。
  const lastSavedRef = useRef<string | null>(null);

  const startEdit = (row: ManifestRow, field: EditableField) => {
    const raw = (row as unknown as Record<string, unknown>)[field];
    setEditingText(raw == null ? "" : String(raw));
    setEditing({ filename: row.filename, field });
  };

  const saveCell = async (row: ManifestRow, field: EditableField) => {
    const key = `${row.filename}#${field}`;
    // Enter 保存后输入框卸载会再触发一次 blur，第二次直接返回
    if (lastSavedRef.current === key) return;
    const raw = (row as unknown as Record<string, unknown>)[field];
    const prev = raw == null ? "" : String(raw);
    const trimmed = editingText.trim();
    lastSavedRef.current = key;
    setEditing(null);
    if (trimmed === prev) {
      lastSavedRef.current = null;
      return; // 无变化
    }
    if (field === "seq" && trimmed !== "" && !/^\d+$/.test(trimmed)) {
      message.error("序号必须是整数");
      lastSavedRef.current = null;
      return;
    }
    try {
      const payload: Record<string, string | number | null> = {};
      if (field === "seq") payload[field] = trimmed === "" ? null : Number(trimmed);
      else payload[field] = trimmed === "" ? null : trimmed;
      const updated = await updateManifestRow(row.filename, payload);
      setRows((prevRows) =>
        prevRows.map((r) => (r.filename === updated.filename ? { ...r, ...updated } : r))
      );
      message.success("已保存");
    } catch (e) {
      message.error(`保存失败：${(e as Error).message || "未知错误"}`);
    } finally {
      lastSavedRef.current = null;
    }
  };

  /** 生成可编辑单元格的 render 函数 */
  const editableRender =
    (field: EditableField, numeric = false) =>
    (_v: unknown, row: ManifestRow) => {
      const isEditing = editing?.filename === row.filename && editing.field === field;
      if (isEditing) {
        if (numeric) {
          return (
            <InputNumber
              size="small"
              autoFocus
              style={{ width: "100%" }}
              value={editingText === "" ? undefined : Number(editingText)}
              onChange={(n) => setEditingText(n == null ? "" : String(n))}
              onPressEnter={() => saveCell(row, field)}
              onBlur={() => saveCell(row, field)}
            />
          );
        }
        return (
          <Input
            size="small"
            autoFocus
            value={editingText}
            onChange={(e) => setEditingText(e.target.value)}
            onPressEnter={() => saveCell(row, field)}
            onBlur={() => saveCell(row, field)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setEditing(null);
            }}
          />
        );
      }
      const v = (row as unknown as Record<string, unknown>)[field] as
        | string
        | number
        | null
        | undefined;
      return (
        <span
          className="editable-cell"
          title="点击编辑"
          onClick={() => startEdit(row, field)}
          style={{
            cursor: "pointer",
            ...(v == null || v === "" ? { color: "#bbb" } : {}),
          }}
        >
          {v == null || v === "" ? "—" : String(v)}
        </span>
      );
    };

  useEffect(() => {
    load(1);
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  // 单独监听 parseReport 变化：解析完成后即使没触发 refreshKey，
  // 也立刻把 actions 合并到当前 rows 上，保证 UI 第一时间反映结果。
  useEffect(() => {
    if (!effectiveParse || effectiveParse.actions.length === 0) return;
    setRows((prev) => applyParseActionsToRows(prev, effectiveParse.actions));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveParse]);

  // 单独监听 chunkReport 变化：切分完成后即使没触发 refreshKey，也立刻合并
  useEffect(() => {
    if (!effectiveChunk || effectiveChunk.actions.length === 0) return;
    setRows((prev) => applyChunkActionsToRows(prev, effectiveChunk.actions));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveChunk]);

  // 单独监听 difyReport 变化：入库完成后即使没触发 refreshKey，也立刻合并
  useEffect(() => {
    if (!effectiveDify || effectiveDify.actions.length === 0) return;
    setRows((prev) => applyDifyActionsToRows(prev, effectiveDify.actions));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveDify]);

  return (
    <Card
      size="small"
      title={`📋 manifest 台账（${total} 行） · 点击「序号」单元格可编辑 · 文档元数据请到「文档元数据」页填写`}
      extra={
        <Button size="small" icon={<ReloadOutlined />} onClick={() => load(page)}>
          刷新
        </Button>
      }
    >
      <Table
        size="small"
        rowKey="filename"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1900 }}
        pagination={{
          current: page,
          pageSize: 50,
          total,
          showSizeChanger: false,
          onChange: (p) => {
            setPage(p);
            load(p);
          },
        }}
        columns={[
          {
            title: "序号",
            dataIndex: "seq",
            width: 60,
            render: editableRender("seq", true),
          },
          { title: "文件名称", dataIndex: "filename", fixed: "left", width: 200, ellipsis: true },
          { title: "导入", dataIndex: "import_status", width: 80 },
          { title: "处理", dataIndex: "process_status", width: 80 },
          {
            title: "parse",
            dataIndex: "parse",
            width: 200,
            ellipsis: true,
            render: (v: string | null | undefined) => {
              if (!v) return <span style={{ color: "#bbb" }}>—</span>;
              const looksLikePath = /[\\/]/.test(v);
              if (looksLikePath) {
                return (
                  <Tooltip title={v}>
                    <Tag color="green" style={{ cursor: "default" }}>
                      {v.replace(/^.*[\\/](parsed[\\/])?/, "")}
                    </Tag>
                  </Tooltip>
                );
              }
              if (v.includes("失败") || v.includes("error")) {
                return <Tag color="red">{v}</Tag>;
              }
              return <Tag>{v}</Tag>;
            },
          },
          {
            title: "chunks",
            dataIndex: "chunks",
            width: 200,
            ellipsis: true,
            render: (v: string | null | undefined) => {
              if (!v) return <span style={{ color: "#bbb" }}>—</span>;
              // 失败/错误描述（任何含「失败」「error」或中文「错」）
              if (
                /失败|error|解析失败|切分失败/i.test(v) ||
                v.toLowerCase().includes("error")
              ) {
                return (
                  <Tooltip title={v}>
                    <Tag color="red">{v}</Tag>
                  </Tooltip>
                );
              }
              // 试运行 / 跳过提示
              if (v.startsWith("试运行") || v.startsWith("切分跳过") || v.startsWith("无")) {
                return <Tag color="gold">{v}</Tag>;
              }
              // 路径形式（含 / 或 \）
              if (/[\\/]/.test(v)) {
                const isArchived = v.startsWith("output/") || v.startsWith("output\\");
                const cleaned = v.replace(/^(data[\\/])?(chunks|output)[\\/]/, "");
                return (
                  <Tooltip title={v}>
                    <Tag
                      color={isArchived ? "gold" : "green"}
                      style={{ cursor: "default" }}
                    >
                      {isArchived ? "📦→" : "📦"} {cleaned}
                    </Tag>
                  </Tooltip>
                );
              }
              // stem 形式（无路径分隔符）= 切分成功
              return (
                <Tooltip title={`data/chunks/${v}/`}>
                  <Tag color="green" style={{ cursor: "default" }}>
                    📦 {v}
                  </Tag>
                </Tooltip>
              );
            },
          },
          {
            title: "dify_doc_id",
            dataIndex: "dify_doc_id",
            width: 220,
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ? (
                <Tooltip title={v}>
                  <code style={{ fontSize: 12 }}>{v}</code>
                </Tooltip>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "dify_status",
            dataIndex: "dify_status",
            width: 110,
            render: (v: string | null | undefined) =>
              v ? (
                <Tag color={DIFY_STATUS_COLORS[v] ?? "default"}>{v}</Tag>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "status",
            dataIndex: "status",
            width: 100,
            render: (v: string | null | undefined) =>
              v ? <Tag color={STATUS_COLORS[v] ?? "default"}>{v}</Tag> : <Tag>—</Tag>,
          },
          {
            title: "md5",
            dataIndex: "md5",
            width: 200,
            render: (v: string | null | undefined) =>
              v ? (
                <Tooltip title={v}>
                  <code style={{ fontSize: 12 }}>{v.slice(0, 12)}…</code>
                </Tooltip>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "create_time",
            dataIndex: "create_time",
            width: 160,
          },
          {
            title: "update_time",
            dataIndex: "update_time",
            width: 160,
            sorter: (a, b) => (a.update_time || "").localeCompare(b.update_time || ""),
            defaultSortOrder: "descend",
          },
          {
            title: "error",
            dataIndex: "error_msg",
            width: 180,
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ? <span style={{ color: "#cf1322" }}>{v}</span> : <span style={{ color: "#bbb" }}>—</span>,
          },
        ]}
      />
    </Card>
  );
}
