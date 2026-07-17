import { useState } from "react";
import "./DataSourceSelector.css";

type DataSourceSelectorProps = {
  selectedSource: string;
  onSourceSelect: (src: string) => void;
};

const OPTIONS = [
  "Local Storage",
  "S3",
  "Google Drive",
  "Dropbox",
];

const DataSourceSelector = ({
  selectedSource,
  onSourceSelect,
}: DataSourceSelectorProps) => {
  const [open, setOpen] = useState(false);

  const toggle = () => setOpen((s) => !s);

  const handleSelect = (opt: string) => {
    onSourceSelect(opt);
    setOpen(false);
  };

  return (
    <div className="source-selector">
      <button
        type="button"
        className="source-select-btn"
        onClick={toggle}
      >
        <span>{selectedSource || "Select Source"}</span>
        <span
          className={`source-arrow ${open ? "arrow-open" : ""}`}
        >
          ▾
        </span>
      </button>

      {open && (
        <div className="source-dropdown">
          {OPTIONS.map((opt) => (
            <button
              key={opt}
              type="button"
              className="source-option"
              onClick={() => handleSelect(opt)}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default DataSourceSelector;