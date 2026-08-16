import React, { useState } from 'react';
import { Copy, Check, ExternalLink } from 'lucide-react';

/**
 * A lightweight, zero-dependency, robust Markdown renderer for analytical briefings,
 * tables, code snippets, and post ID citations.
 */
export default function MarkdownView({ content, className = '' }) {
  if (!content) return null;

  const parsedBlocks = parseMarkdownBlocks(content);

  return (
    <div className={`space-y-3 leading-relaxed text-slate-800 dark:text-zinc-200 text-sm ${className}`}>
      {parsedBlocks.map((block, idx) => (
        <RenderBlock key={idx} block={block} />
      ))}
    </div>
  );
}

function RenderBlock({ block }) {
  if (block.type === 'heading') {
    const Tag = `h${Math.min(block.level, 6)}`;
    const sizes = {
      1: 'text-xl font-bold mt-4 mb-2 pb-1 border-b border-slate-200 dark:border-zinc-800 text-slate-900 dark:text-zinc-100',
      2: 'text-lg font-bold mt-3 mb-1.5 text-slate-900 dark:text-zinc-100',
      3: 'text-base font-semibold mt-2 mb-1 text-slate-900 dark:text-zinc-100',
      4: 'text-sm font-semibold mt-1.5 mb-1 text-slate-800 dark:text-zinc-200',
      5: 'text-xs font-semibold uppercase tracking-wider text-slate-500',
      6: 'text-xs font-semibold text-slate-500',
    };
    return (
      <Tag className={sizes[block.level] || sizes[3]}>
        <InlineContent text={block.text} />
      </Tag>
    );
  }

  if (block.type === 'codeblock') {
    return <CodeBlock lang={block.lang} code={block.code} />;
  }

  if (block.type === 'table') {
    return <TableView headers={block.headers} rows={block.rows} />;
  }

  if (block.type === 'quote') {
    return (
      <blockquote className="border-l-4 border-brand-500/80 bg-brand-50/40 dark:bg-brand-950/20 px-4 py-2 rounded-r-lg italic text-slate-700 dark:text-zinc-300 my-2">
        <InlineContent text={block.text} />
      </blockquote>
    );
  }

  if (block.type === 'list') {
    const ListTag = block.ordered ? 'ol' : 'ul';
    const listClass = block.ordered
      ? 'list-decimal list-inside space-y-1 my-2 pl-2'
      : 'list-disc list-inside space-y-1 my-2 pl-2';

    return (
      <ListTag className={listClass}>
        {block.items.map((item, i) => (
          <li key={i} className="text-slate-700 dark:text-zinc-300">
            <InlineContent text={item} />
          </li>
        ))}
      </ListTag>
    );
  }

  if (block.type === 'hr') {
    return <hr className="my-4 border-slate-200 dark:border-zinc-800" />;
  }

  // Paragraph
  return (
    <p className="my-1.5">
      <InlineContent text={block.text} />
    </p>
  );
}

function CodeBlock({ lang, code }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative my-3 rounded-xl overflow-hidden border border-slate-800 bg-[#1e1e24] dark:bg-[#0c0c0e] shadow-sm">
      <div className="flex items-center justify-between px-4 py-1.5 bg-slate-900/90 border-b border-slate-800 text-xs text-slate-400 font-mono">
        <span>{lang || 'code'}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 hover:text-slate-200 transition-colors py-0.5 px-1.5 rounded"
          title="Copy code"
        >
          {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <pre className="p-4 overflow-x-auto text-xs font-mono text-slate-200 leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function TableView({ headers, rows }) {
  return (
    <div className="my-3 overflow-x-auto rounded-xl border border-slate-200 dark:border-zinc-800 shadow-sm">
      <table className="w-full text-left border-collapse text-xs">
        {headers && headers.length > 0 && (
          <thead>
            <tr className="bg-slate-100 dark:bg-zinc-900/80 border-b border-slate-200 dark:border-zinc-800 font-semibold text-slate-700 dark:text-zinc-200">
              {headers.map((h, i) => (
                <th key={i} className="px-3.5 py-2.5 whitespace-nowrap">
                  <InlineContent text={h} />
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody className="divide-y divide-slate-100 dark:divide-zinc-800/60 bg-white dark:bg-[#09090b]">
          {rows.map((row, rIdx) => (
            <tr key={rIdx} className="hover:bg-slate-50/80 dark:hover:bg-zinc-900/40 transition-colors">
              {row.map((cell, cIdx) => (
                <td key={cIdx} className="px-3.5 py-2 text-slate-700 dark:text-zinc-300">
                  <InlineContent text={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function InlineContent({ text }) {
  if (!text) return null;

  // Split and handle formatting:
  // 1. Post IDs: cm0[a-z0-9]{20,}
  // 2. Bold: **text**
  // 3. Inline code: `text`
  // 4. Italic: *text* or _text_

  const tokens = tokenizeInline(text);

  return (
    <>
      {tokens.map((tok, i) => {
        if (tok.type === 'bold') {
          return <strong key={i} className="font-semibold text-slate-900 dark:text-zinc-100">{tok.value}</strong>;
        }
        if (tok.type === 'code') {
          return (
            <code key={i} className="font-mono text-[11px] bg-slate-100 dark:bg-zinc-800/90 text-slate-800 dark:text-zinc-200 px-1.5 py-0.5 rounded border border-slate-200 dark:border-zinc-700/60">
              {tok.value}
            </code>
          );
        }
        if (tok.type === 'post_id') {
          return (
            <span
              key={i}
              className="inline-flex items-center gap-1 font-mono text-[11px] bg-brand-50 dark:bg-brand-950/40 text-brand-700 dark:text-brand-300 px-1.5 py-0.5 rounded border border-brand-200 dark:border-brand-800/60 font-medium"
              title={`Post ID: ${tok.value}`}
            >
              {tok.value}
            </span>
          );
        }
        if (tok.type === 'italic') {
          return <em key={i} className="italic">{tok.value}</em>;
        }
        return tok.value;
      })}
    </>
  );
}

function tokenizeInline(text) {
  const result = [];
  let remaining = text;

  // Regex to match special inline tokens
  const regex = /(\*\*(.+?)\*\*|`([^`]+)`|(cm[a-z0-9]{20,})|\*([^*]+)\*)/;

  while (remaining) {
    const match = remaining.match(regex);
    if (!match) {
      result.push({ type: 'text', value: remaining });
      break;
    }

    const matchIndex = match.index;
    if (matchIndex > 0) {
      result.push({ type: 'text', value: remaining.slice(0, matchIndex) });
    }

    const fullMatch = match[0];
    if (fullMatch.startsWith('**') && fullMatch.endsWith('**')) {
      result.push({ type: 'bold', value: match[2] });
    } else if (fullMatch.startsWith('`') && fullMatch.endsWith('`')) {
      result.push({ type: 'code', value: match[3] });
    } else if (match[4]) {
      result.push({ type: 'post_id', value: match[4] });
    } else if (fullMatch.startsWith('*') && fullMatch.endsWith('*')) {
      result.push({ type: 'italic', value: match[5] });
    } else {
      result.push({ type: 'text', value: fullMatch });
    }

    remaining = remaining.slice(matchIndex + fullMatch.length);
  }

  return result;
}

function parseMarkdownBlocks(rawText) {
  const lines = rawText.split('\n');
  const blocks = [];
  let inCodeBlock = false;
  let codeLang = '';
  let codeLines = [];
  let inTable = false;
  let tableHeaders = [];
  let tableRows = [];
  let inList = false;
  let listOrdered = false;
  let listItems = [];

  const flushTable = () => {
    if (inTable) {
      blocks.push({ type: 'table', headers: tableHeaders, rows: tableRows });
      tableHeaders = [];
      tableRows = [];
      inTable = false;
    }
  };

  const flushList = () => {
    if (inList) {
      blocks.push({ type: 'list', ordered: listOrdered, items: listItems });
      listItems = [];
      inList = false;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    // Code block toggle
    if (trimmed.startsWith('```')) {
      flushTable();
      flushList();
      if (inCodeBlock) {
        blocks.push({ type: 'codeblock', lang: codeLang, code: codeLines.join('\n') });
        inCodeBlock = false;
        codeLang = '';
        codeLines = [];
      } else {
        inCodeBlock = true;
        codeLang = trimmed.slice(3).trim();
        codeLines = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    // Horizontal Rule
    if (trimmed === '---' || trimmed === '***' || trimmed === '___') {
      flushTable();
      flushList();
      blocks.push({ type: 'hr' });
      continue;
    }

    // Table row
    if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
      flushList();
      const cells = trimmed
        .slice(1, -1)
        .split('|')
        .map(c => c.trim());

      // Check if separator line (|---|---|)
      const isSeparator = cells.every(c => /^:?-+:?$/.test(c));
      if (isSeparator) {
        // Just marks that previous row was header
        continue;
      }

      if (!inTable) {
        inTable = true;
        tableHeaders = cells;
      } else {
        tableRows.push(cells);
      }
      continue;
    } else {
      flushTable();
    }

    // Headings
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      flushList();
      blocks.push({
        type: 'heading',
        level: headingMatch[1].length,
        text: headingMatch[2],
      });
      continue;
    }

    // Blockquote
    if (trimmed.startsWith('>')) {
      flushList();
      blocks.push({
        type: 'quote',
        text: trimmed.replace(/^>\s*/, ''),
      });
      continue;
    }

    // Unordered List
    const bulletMatch = trimmed.match(/^[-*•]\s+(.*)$/);
    if (bulletMatch) {
      if (!inList || listOrdered) {
        flushList();
        inList = true;
        listOrdered = false;
      }
      listItems.push(bulletMatch[1]);
      continue;
    }

    // Ordered List
    const orderedMatch = trimmed.match(/^(\d+)\.\s+(.*)$/);
    if (orderedMatch) {
      if (!inList || !listOrdered) {
        flushList();
        inList = true;
        listOrdered = true;
      }
      listItems.push(orderedMatch[2]);
      continue;
    }

    flushList();

    // Empty line
    if (!trimmed) {
      continue;
    }

    // Setext heading: a text line underlined with === (h1) or --- (h2).
    //
    // The agents emit these, and without this branch the underline fell through
    // to the paragraph case and rendered as a literal row of "=" characters in
    // the middle of a briefing. Checked here, at the point where the line is
    // already known to be an ordinary paragraph, so a list item or table row
    // followed by dashes cannot be swallowed as a heading. The underline is
    // consumed with i++ so the "---" form is not then re-read as a horizontal
    // rule; requiring two dashes keeps a lone "-" bullet out of it.
    const underline = (lines[i + 1] || '').trim();
    if (/^=+$/.test(underline) || /^-{2,}$/.test(underline)) {
      blocks.push({
        type: 'heading',
        level: underline[0] === '=' ? 1 : 2,
        text: trimmed,
      });
      i++;
      continue;
    }

    // Standard Paragraph
    blocks.push({ type: 'paragraph', text: line });
  }

  flushTable();
  flushList();
  if (inCodeBlock) {
    blocks.push({ type: 'codeblock', lang: codeLang, code: codeLines.join('\n') });
  }

  return blocks;
}
