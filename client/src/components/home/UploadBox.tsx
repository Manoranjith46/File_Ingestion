import { useRef, useState } from "react";
import "./UploadBox.css";

type UploadBoxProps = {
  onFilesSelected: (
    files: FileList
  ) => void;
};

const UploadBox = ({
  onFilesSelected,
}: UploadBoxProps) => {
  const fileInputRef =
    useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] =
    useState<FileList | null>(null);

  const handleBrowse = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (
    event: React.ChangeEvent<HTMLInputElement>
  ) => {
    if (event.target.files) {
      setSelectedFiles(event.target.files);
    }
  };

  const handleUpload = () => {
    if (!selectedFiles) return;

    onFilesSelected(selectedFiles);
    setSelectedFiles(null);

    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const selectedCount =
    selectedFiles ? selectedFiles.length : 0;

  return (
    <div className="upload-box">
      <div className="upload-box-icon">
        ↑
      </div>

      <div className="upload-content">
        <h3>Local Storage</h3>

        <p>
          Upload files from your device
        </p>
      </div>

      <div className="upload-drop-area">
        <span>⇧</span>

        <p>
          {selectedCount > 0 ? (
            `${selectedCount} file${selectedCount > 1 ? "s" : ""} selected`
          ) : (
            <>
              Drag & Drop Files or{" "}
              <button
                type="button"
                onClick={handleBrowse}
              >
                Browse Files
              </button>
            </>
          )}
        </p>
      </div>

      {selectedCount > 0 && (
        <button
          type="button"
          className="upload-now-btn"
          onClick={handleUpload}
        >
          Upload
        </button>
      )}

      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        onChange={handleFileChange}
      />
    </div>
  );
};

export default UploadBox;