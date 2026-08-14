import type { ReactNode } from 'react';

type StickyTableToolbarProps = {
  summary: ReactNode;
  children?: ReactNode;
  className?: string;
};

export function StickyTableToolbar({ summary, children, className = '' }: StickyTableToolbarProps) {
  return (
    <div className={`sticky-table-toolbar ${className}`.trim()}>
      <div className="sticky-table-toolbar__summary">{summary}</div>
      {children ? <div className="sticky-table-toolbar__actions">{children}</div> : null}
    </div>
  );
}
