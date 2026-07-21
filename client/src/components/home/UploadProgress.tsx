import "./UploadProgress.css";
import type { UploadedFile } from "./FileTable";

type UploadProgressProps = {
  files: UploadedFile[];
};

const UploadProgress = ({
  files,
}: UploadProgressProps) => {
  const total = files.length;

  const completed = files.filter(
    (file) => file.status === "Done"
  ).length;

  const activeFile = files.find(
    (file) => file.status !== "Done"
  );

  const progress = activeFile
    ? activeFile.progress
    : completed > 0
      ? 100
      : 0;

  const isCompleted =
    total > 0 && completed === total;

  return (
    <div className="upload-progress-card">
      <div className="progress-header">
        <div>
          <span className="progress-title">
            Upload Progress
          </span>

          <span className="progress-subtitle">
            {isCompleted
              ? "Upload completed"
              : "Uploading files"}
          </span>
        </div>

        <span className="progress-count">
          {completed}/{total}
        </span>
      </div>

      <div className="progress-track">
        <div
          className="progress-value"
          style={{
            width: `${progress}%`,
          }}
        />
      </div>

      <div className="progress-footer">
        <span>
          {isCompleted
            ? "✓ All files uploaded"
            : `${progress}% completed`}
        </span>

        <span>
          {isCompleted
            ? "Done"
            : "In progress"}
        </span>
      </div>
    </div>
  );
};

export default UploadProgress;