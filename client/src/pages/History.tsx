import { useEffect, useState } from "react";
import "./History.css";

type HistoryItem = {
  id: string;
  name: string;
  size: string;
  source: string;
  status: string;
  uploadedAt: string;
  uploadedBy?: string;
};

const History = () => {
  const [items, setItems] = useState<HistoryItem[]>([]);

  useEffect(() => {
    try {
      const json = localStorage.getItem("ingester-history");
      setItems(json ? JSON.parse(json) : []);
    } catch {
      setItems([]);
    }
  }, []);

  return (
    <div className="history-page">
      <h2>Upload History</h2>

      {items.length === 0 ? (
        <p>No upload history available.</p>
      ) : (
        <table className="history-table">
          <thead>
            <tr>
              <th>File Name</th>
              <th>Size</th>
              <th>Source</th>
              <th>Uploaded By</th>
              <th>Date / Time</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.id}>
                <td>{it.name}</td>
                <td>{it.size}</td>
                <td>{it.source}</td>
                <td>{it.uploadedBy || "-"}</td>
                <td>{new Date(it.uploadedAt).toLocaleString()}</td>
                <td>{it.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

export default History;
