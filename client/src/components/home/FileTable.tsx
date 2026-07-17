import "./FileTable.css";

export type UploadedFile = {
  id: string;
  name: string;
  size: string;
  source: string;
  status: string;
  progress?: number; // 0-100
  totalChunks?: number;
};

type FileTableProps = {
  files: UploadedFile[];
};

const FileTable = ({
  files,
}: FileTableProps) => {
  return (
    <div className="file-table-wrapper">
      <table className="file-table">
        <thead>
          <tr>
            <th>File Name</th>
            <th>Size</th>
            <th>Source</th>
            <th>Status</th>
          </tr>
        </thead>

        <tbody>
          {files.length === 0 ? (
            <tr>
              <td
                colSpan={4}
                className="empty-row"
              >
                No files uploaded yet
              </td>
            </tr>
          ) : (
            files.map((file) => (
              <tr key={file.id}>
                <td>{file.name}</td>

                <td>{file.size}</td>

                <td>{file.source}</td>

                <td>
                  <span
                    className={
                      file.status === "Done"
                        ? "status-done"
                        : "status-pending"
                    }
                  >
                    {file.status}
                  </span>
                </td>

                {/* progress removed from table; shown in mini box */}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
};

export default FileTable;