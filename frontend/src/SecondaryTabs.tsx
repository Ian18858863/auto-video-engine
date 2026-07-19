import type { ReactNode } from "react";

export interface SecondaryTabItem<T extends string> {
  id: T;
  label: string;
}

interface SecondaryTabsProps<T extends string> {
  label: string;
  items: Array<SecondaryTabItem<T>>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
}

function SecondaryTabs<T extends string>({
  label,
  items,
  value,
  onChange,
  className = "",
}: SecondaryTabsProps<T>) {
  return (
    <nav
      className={`secondary-tabs ${className}`.trim()}
      role="tablist"
      aria-label={label}
    >
      {items.map((item) => (
        <button
          key={item.id}
          className={
            value === item.id ? "secondary-tab active" : "secondary-tab"
          }
          type="button"
          role="tab"
          aria-selected={value === item.id}
          onClick={() => onChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}

export function TabContextNote({ children }: { children: ReactNode }) {
  return <p className="tab-context-note">{children}</p>;
}

export default SecondaryTabs;
