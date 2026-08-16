import React from 'react';
import { render, screen } from '@testing-library/react';
import MarkdownView from './MarkdownView.jsx';

// The agents write setext headings — a title underlined with "=" or "-" — and
// the parser only understood the ATX form ("## Title"). The underline fell
// through to the paragraph branch, so a live briefing rendered as:
//
//   Sentiment Distribution Comparison
//   =====================================
//
// with the equals signs printed as body text in the middle of an intelligence
// report. The prompt now asks for ATX, but a prompt is advisory; this is the
// half that cannot be disobeyed.

describe('MarkdownView setext headings', () => {
  it('renders an "=" underlined line as a heading, not as equals signs', () => {
    const { container } = render(
      <MarkdownView content={'Sentiment Distribution Comparison\n=====================================\n\nBody text.'} />
    );

    const heading = screen.getByText('Sentiment Distribution Comparison');
    expect(heading.tagName).toBe('H1');
    expect(container.textContent).not.toContain('===');
  });

  it('renders a "-" underlined line as a level-2 heading', () => {
    render(<MarkdownView content={'Engagement Comparison\n---------------------\n\nBody.'} />);

    expect(screen.getByText('Engagement Comparison').tagName).toBe('H2');
  });

  it('still renders ATX headings', () => {
    render(<MarkdownView content={'## Executive Findings\n\nBody.'} />);

    expect(screen.getByText('Executive Findings').tagName).toBe('H2');
  });

  it('keeps a horizontal rule a horizontal rule when nothing precedes it', () => {
    // The runner appends "\n\n---\n**Unverified citations.**" — a blank line
    // sits above the dashes, so this must stay an <hr> and not eat the text.
    const { container } = render(
      <MarkdownView content={'A finding.\n\n---\n\n**Unverified citations.** none'} />
    );

    expect(container.querySelector('hr')).not.toBeNull();
    expect(container.textContent).toContain('Unverified citations.');
  });

  it('does not turn a table separator into a heading', () => {
    const { container } = render(
      <MarkdownView content={'| Post ID | Reactions |\n|---|---|\n| cmosjpp9305n0u9tskgmd1c4k | 84979 |'} />
    );

    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelectorAll('h1, h2').length).toBe(0);
  });

  it('does not turn a list item followed by dashes into a heading', () => {
    const { container } = render(<MarkdownView content={'- first point\n---\n'} />);

    expect(container.querySelector('li')).not.toBeNull();
    expect(container.querySelectorAll('h1, h2').length).toBe(0);
  });

  it('leaves "=" inside a code block alone', () => {
    const { container } = render(
      <MarkdownView content={'```\ntitle\n=====\n```'} />
    );

    expect(container.querySelectorAll('h1').length).toBe(0);
    expect(container.textContent).toContain('=====');
  });

  it('renders the shape of the briefing that exposed this', () => {
    const briefing = [
      'Sentiment Distribution Comparison',
      '=====================================',
      '',
      'The sentiment distribution across top posts is as follows:',
      '',
      '| Period | Positive | Negative |',
      '|---|---|---|',
      '| 2026-04-29 | 0 | 2 |',
      '',
      'Engagement Comparison',
      '=========================',
      '',
      '| Post ID | Total Reactions |',
      '|---|---|',
      '| cmosjpp9305n0u9tskgmd1c4k | 84979 |',
    ].join('\n');

    const { container } = render(<MarkdownView content={briefing} />);

    expect(container.querySelectorAll('h1').length).toBe(2);
    expect(container.querySelectorAll('table').length).toBe(2);
    expect(container.textContent).not.toContain('==');
  });
});
