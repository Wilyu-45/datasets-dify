import React, { useMemo } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

/**
 * 轻量 Markdown 渲染组件（仅依赖 KaTeX 渲染公式）。
 * 支持：标题 / 段落 / 列表 / 引用 / 代码块 / 分隔线 /
 *      管道表格（含无分隔行表格）/ HTML <table> /
 *      行内代码 / 粗体 / 斜体 / 删除线 / 链接 / 图片 /
 *      行内公式 $...$、\(...\) 与块级公式 $$...$$、\[...\]。
 * 图片使用 <img> 直接渲染（可加载 OSS 等公网地址）。
 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** KaTeX 公式渲染（失败时原样显示） */
function Formula({
  latex,
  displayMode,
}: {
  latex: string;
  displayMode?: boolean;
}) {
  let html = "";
  try {
    html = katex.renderToString(latex, {
      throwOnError: false,
      displayMode: !!displayMode,
    });
  } catch {
    html = `<code>${escapeHtml(latex)}</code>`;
  }
  return (
    <span
      className="md-formula"
      style={
        displayMode
          ? {
              display: "block",
              margin: "8px 0",
              textAlign: "center",
              overflowX: "auto",
            }
          : undefined
      }
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

/** 行内解析：图片 / 链接 / 代码 / 公式 / 粗体 / 斜体 / 删除线 */
function renderInline(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // 注意：图片/链接（含括号）、公式优先于粗体/斜体匹配
  const tokenRe =
    /(`[^`\n]+`)|(\$\$[^$\n]+\$\$)|(\$[^$\n]+\$)|(\\\([^\\]+\\\))|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(~~[^~\n]+~~)|(!\[[^\]]*\]\([^)\s]+\))|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = tokenRe.exec(text)) !== null) {
    if (m.index > last) nodes.push(escapeHtml(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("`")) {
      nodes.push(
        <code
          key={k++}
          style={{
            background: "#f0f0f0",
            padding: "1px 4px",
            borderRadius: 3,
            fontSize: "0.92em",
          }}
        >
          {tok.slice(1, -1)}
        </code>
      );
    } else if (tok.startsWith("$$")) {
      nodes.push(<Formula key={k++} latex={tok.slice(2, -2)} />);
    } else if (tok.startsWith("$")) {
      nodes.push(<Formula key={k++} latex={tok.slice(1, -1)} />);
    } else if (tok.startsWith("\\(")) {
      nodes.push(<Formula key={k++} latex={tok.slice(2, -2)} />);
    } else if (tok.startsWith("**")) {
      nodes.push(<strong key={k++}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("~~")) {
      nodes.push(<del key={k++}>{tok.slice(2, -2)}</del>);
    } else if (tok.startsWith("*")) {
      nodes.push(<em key={k++}>{tok.slice(1, -1)}</em>);
    } else if (tok.startsWith("![")) {
      const close = tok.indexOf("](");
      const alt = tok.slice(2, close);
      const url = tok.slice(close + 2, tok.length - 1);
      nodes.push(
        <span key={k++} style={{ display: "block", margin: "8px 0", textAlign: "center" }}>
          <img
            src={url}
            alt={alt}
            loading="lazy"
            style={{ maxWidth: "100%", maxHeight: 320, objectFit: "contain" }}
          />
        </span>
      );
    } else if (tok.startsWith("[")) {
      const close = tok.indexOf("](");
      const label = tok.slice(1, close);
      const href = tok.slice(close + 2, tok.length - 1);
      nodes.push(
        <a key={k++} href={href} target="_blank" rel="noreferrer">
          {label}
        </a>
      );
    }
    last = tokenRe.lastIndex;
  }
  if (last < text.length) nodes.push(escapeHtml(text.slice(last)));
  return nodes;
}

/** 表格单元格拆分（去掉首尾空单元格） */
function splitRow(line: string): string[] {
  const cells = line.split("|").map((c) => c.trim());
  if (cells.length > 0 && cells[0] === "") cells.shift();
  if (cells.length > 0 && cells[cells.length - 1] === "") cells.pop();
  return cells;
}

const tableCellStyle = {
  border: "1px solid #e8e8e8",
  padding: "4px 8px",
  fontSize: 12,
} as const;

const tableHeadStyle = {
  border: "1px solid #d9d9d9",
  background: "#fafafa",
  padding: "4px 8px",
  textAlign: "left" as const,
  whiteSpace: "nowrap",
  fontSize: 12,
} as const;

interface HtmlCell {
  text: string;
  rowspan: number;
  colspan: number;
  head: boolean;
}

/** 渲染 HTML <table>：支持 rowspan / colspan 合并单元格，其余标签剥离 */
function renderHtmlTable(html: string, key: number): React.ReactNode {
  // 1) 解析 <tr>/<t[hd]>（提取 rowspan/colspan 属性）
  const parsedRows: HtmlCell[][] = [];
  const trRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
  let m: RegExpExecArray | null;
  while ((m = trRe.exec(html)) !== null) {
    const cells: HtmlCell[] = [];
    const cellRe = /<t(h|d)([^>]*)>([\s\S]*?)<\/t\1>/gi;
    let cm: RegExpExecArray | null;
    while ((cm = cellRe.exec(m[1])) !== null) {
      const attrs = cm[2] ?? "";
      const num = (name: string) => {
        const v = new RegExp(`${name}\\s*=\\s*["']?(\\d+)`, "i").exec(attrs)?.[1];
        return v ? parseInt(v, 10) : 1;
      };
      cells.push({
        text: cm[3]
          .replace(/<br\s*\/?>/gi, "\n")
          .replace(/<[^>]+>/g, "")
          .trim(),
        rowspan: Math.max(1, num("rowspan")),
        colspan: Math.max(1, num("colspan")),
        head: cm[1].toLowerCase() === "th",
      });
    }
    parsedRows.push(cells);
  }
  if (parsedRows.length === 0) return null;

  // 2) 布局矩阵：grid[row][col] = HtmlCell（起始格）| null（被合并占位）| undefined（空槽）
  const grid: (HtmlCell | null | undefined)[][] = [];
  let rowIndex = 0;
  for (const row of parsedRows) {
    const r = rowIndex++;
    while (grid.length <= r) grid.push([]); // 前面的 rowspan 可能已创建本行
    let c = 0;
    for (const cell of row) {
      while (grid[r][c] !== undefined) c++; // 跳过已被占用 / 占位的列
      for (let rr = 0; rr < cell.rowspan; rr++) {
        const rrIdx = r + rr;
        while (grid.length <= rrIdx) grid.push([]);
        for (let cc = 0; cc < cell.colspan; cc++) {
          const ccIdx = c + cc;
          while (grid[rrIdx].length <= ccIdx) grid[rrIdx].push(undefined);
          grid[rrIdx][ccIdx] = rr === 0 && cc === 0 ? cell : null;
        }
      }
    }
  }

  // 3) 渲染：第一行作表头（含 <th> 或纯 <td>），rowspan/colspan 映射为合并属性
  const headRow = grid[0] ?? [];
  const bodyRows = grid.slice(1);

  const renderCell = (cell: HtmlCell | null | undefined, idx: number) => {
    if (cell === null) return null; // 被上方 rowspan 合并占位
    if (cell === undefined) return <td key={idx} style={tableCellStyle} />;
    const Tag = (cell.head ? "th" : "td") as "th" | "td";
    return (
      <Tag
        key={idx}
        rowSpan={cell.rowspan > 1 ? cell.rowspan : undefined}
        colSpan={cell.colspan > 1 ? cell.colspan : undefined}
        style={cell.head ? tableHeadStyle : tableCellStyle}
      >
        {renderInline(cell.text)}
      </Tag>
    );
  };

  return (
    <div key={key} style={{ overflowX: "auto", margin: "8px 0" }}>
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr>{headRow.map((cell, idx) => renderCell(cell, idx))}</tr>
        </thead>
        <tbody>
          {bodyRows.map((row, ri) => (
            <tr key={ri}>{row.map((cell, idx) => renderCell(cell, idx))}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MarkdownPreview({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  const blocks = useMemo(() => {
    const lines = content.split("\n");
    const out: React.ReactNode[] = [];
    let i = 0;
    let k = 0;
    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      // 代码块
      if (trimmed.startsWith("```")) {
        const buf: string[] = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith("```")) {
          buf.push(lines[i]);
          i++;
        }
        i++; // 跳过结尾 ```（不存在则越界由循环结束兜底）
        out.push(
          <pre
            key={k++}
            style={{
              background: "#f5f5f5",
              padding: 8,
              borderRadius: 4,
              overflowX: "auto",
              fontSize: 12,
            }}
          >
            <code>{buf.join("\n")}</code>
          </pre>
        );
        continue;
      }

      // 块级公式：$$...$$（可跨多行）
      if (trimmed.startsWith("$$")) {
        const buf: string[] = [line];
        i++;
        if (!/^\s*\$\$[\s\S]*\$\$\s*$/.test(line)) {
          while (i < lines.length && !/^\s*\$\$/.test(lines[i])) {
            buf.push(lines[i]);
            i++;
          }
          if (i < lines.length) buf.push(lines[i]);
          i++;
        }
        const latex = buf
          .join("\n")
          .replace(/^\s*\$\$/, "")
          .replace(/\$\$\s*$/, "");
        out.push(<Formula key={k++} latex={latex} displayMode />);
        continue;
      }

      // 块级公式：\[...\]
      if (/^\s*\\\[/.test(trimmed)) {
        const buf: string[] = [line];
        i++;
        while (i < lines.length && !/\\\]/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        if (i < lines.length) buf.push(lines[i]);
        i++;
        const latex = buf
          .join("\n")
          .replace(/^\s*\\\[/, "")
          .replace(/\\\]\s*$/, "");
        out.push(<Formula key={k++} latex={latex} displayMode />);
        continue;
      }

      // HTML 表格（<table> 与 </table> 可同行或跨多行）
      if (/^\s*<table/i.test(trimmed)) {
        const buf: string[] = [line];
        i++;
        if (!/<\/table>/i.test(line)) {
          while (i < lines.length && !/<\/table>/i.test(lines[i])) {
            buf.push(lines[i]);
            i++;
          }
          if (i < lines.length) buf.push(lines[i]);
          i++;
        }
        out.push(renderHtmlTable(buf.join("\n"), k++));
        continue;
      }

      // 标题
      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        const level = Math.min(h[1].length, 4) as 1 | 2 | 3 | 4;
        const Tag = `h${level}` as keyof JSX.IntrinsicElements;
        out.push(
          <Tag
            key={k++}
            style={{
              margin: "10px 0 4px",
              borderBottom: "1px solid #eee",
              paddingBottom: 2,
            }}
          >
            {renderInline(h[2])}
          </Tag>
        );
        i++;
        continue;
      }

      // 引用
      if (/^>\s?/.test(line)) {
        const buf: string[] = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^>\s?/, ""));
          i++;
        }
        out.push(
          <blockquote
            key={k++}
            style={{
              margin: "6px 0",
              padding: "4px 10px",
              borderLeft: "3px solid #d9d9d9",
              color: "#595959",
              background: "#fafafa",
            }}
          >
            {renderInline(buf.join("\n"))}
          </blockquote>
        );
        continue;
      }

      // 列表（连续行）
      const listRe = /^\s*([-*+]|\d+\.)\s+(.*)$/;
      if (listRe.test(line)) {
        const items: string[] = [];
        while (i < lines.length) {
          const mm = listRe.exec(lines[i]);
          if (!mm) break;
          items.push(mm[2]);
          i++;
        }
        out.push(
          <ul key={k++} style={{ margin: "6px 0", paddingLeft: 20 }}>
            {items.map((it, j) => (
              <li key={j} style={{ margin: "2px 0" }}>
                {renderInline(it)}
              </li>
            ))}
          </ul>
        );
        continue;
      }

      // 表格：标准 markdown（下一行是分隔行）或连续多行含 | 的行组
      const isSepRow =
        i + 1 < lines.length &&
        /^\s*\|?\s*:?-+[\s|:-]*$/.test(lines[i + 1]);
      if (line.includes("|") && (isSepRow || (lines[i + 1] ?? "").includes("|"))) {
        const header = splitRow(line);
        i += isSepRow ? 2 : 1;
        const rows: string[][] = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
          rows.push(splitRow(lines[i]));
          i++;
        }
        const maxCols = Math.max(header.length, ...rows.map((r) => r.length));
        out.push(
          <div key={k++} style={{ overflowX: "auto", margin: "8px 0" }}>
            <table style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr>
                  {Array.from({ length: maxCols }).map((_, c) => (
                    <th key={c} style={tableHeadStyle}>
                      {renderInline(header[c] ?? "")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, r) => (
                  <tr key={r}>
                    {Array.from({ length: maxCols }).map((_, c) => (
                      <td key={c} style={tableCellStyle}>
                        {renderInline(row[c] ?? "")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
        continue;
      }

      // 分隔线
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        out.push(<hr key={k++} style={{ margin: "8px 0", border: "none", borderTop: "1px solid #e8e8e8" }} />);
        i++;
        continue;
      }

      // 空行
      if (trimmed === "") {
        i++;
        continue;
      }

      // 段落：收集连续的普通行
      const buf: string[] = [line];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        !/^(#{1,6}\s|```|>\s?|\s*([-*+]|\d+\.)\s+)/.test(lines[i]) &&
        !/^\s*<table/i.test(lines[i]) &&
        !lines[i].includes("|")
      ) {
        buf.push(lines[i]);
        i++;
      }
      out.push(
        <p key={k++} style={{ margin: "6px 0" }}>
          {renderInline(buf.join("\n"))}
        </p>
      );
    }
    return out;
  }, [content]);

  return (
    <div className={className} style={{ wordBreak: "break-word" }}>
      {blocks.length === 0 ? (
        <span style={{ color: "#999" }}>(空内容)</span>
      ) : (
        blocks
      )}
    </div>
  );
}
