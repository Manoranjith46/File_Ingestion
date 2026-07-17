import { useRef, useState } from "react";

//import Header from "../components/home/Header";
import Sidebar from "../components/home/Sidebar";
import DataSourceSelector from "../components/home/DataSourceSelector";
import UploadBox from "../components/home/UploadBox";
import FileTable from "../components/home/FileTable";
import type { UploadedFile } from "../components/home/FileTable";
import UploadProgress from "../components/home/UploadProgress";
import History from "./History";
import Profile from "./Profile";

import "./Home.css";

type HomeProps = {
  onLogout: () => void;
};

const Home = ({ onLogout }: HomeProps) => {
  const [selectedSource, setSelectedSource] =
    useState("Local Storage");

  const [activeMenu, setActiveMenu] =
    useState("Home");

  const [files, setFiles] = useState<
    UploadedFile[]
  >([]);

  const filesRef = useRef<Record<string, File>>({});

  const CHUNK_SIZE = 5 * 1024 * 1024;

  const fakeUpload = (_chunk: Blob) => {
    return new Promise<void>((res) =>
      setTimeout(() => res(), 300)
    );
  };

  const formatFileSize = (
    bytes: number
  ) => {
    if (bytes < 1024) {
      return `${bytes} B`;
    }

    if (bytes < 1024 * 1024) {
      return `${(
        bytes / 1024
      ).toFixed(1)} KB`;
    }

    return `${(
      bytes /
      (1024 * 1024)
    ).toFixed(1)} MB`;
  };

  const uploadFileChunks = async (
    file: File,
    id: string
  ) => {
    const totalChunks = Math.ceil(
      file.size / CHUNK_SIZE
    );

    for (let i = 0; i < totalChunks; i++) {
      const start = i * CHUNK_SIZE;
      const end = Math.min(
        start + CHUNK_SIZE,
        file.size
      );
      const chunk = file.slice(start, end);

      try {
        await fakeUpload(chunk);

        const progress = Math.round(
          ((i + 1) / totalChunks) * 100
        );

        setFiles((prev) =>
          prev.map((f) =>
            f.id === id
              ? {
                  ...f,
                  progress,
                  totalChunks,
                  status:
                    progress === 100
                      ? "Done"
                      : "Uploading",
                }
              : f
          )
        );

        if (progress === 100) {
          const historyItem = {
            id,
            name: file.name,
            size: formatFileSize(file.size),
            source: "Local",
            status: "Done",
            uploadedAt: new Date().toISOString(),
            uploadedBy:
              localStorage.getItem(
                "ingester-current-user"
              ) || "-",
          };

          try {
            const histJson =
              localStorage.getItem(
                "ingester-history"
              );
            const hist = histJson
              ? JSON.parse(histJson)
              : [];

            hist.unshift(historyItem);
            localStorage.setItem(
              "ingester-history",
              JSON.stringify(hist)
            );
          } catch {
            localStorage.setItem(
              "ingester-history",
              JSON.stringify([historyItem])
            );
          }
        }
      } catch {
        setFiles((prev) =>
          prev.map((f) =>
            f.id === id
              ? {
                  ...f,
                  status: "Error",
                }
              : f
          )
        );
        break;
      }
    }
  };

  const handleFilesSelected = (
    selectedFiles: FileList
  ) => {
    const incoming: UploadedFile[] = [];
    const selectedNames = new Set(
      files.map((file) => file.name.toLowerCase())
    );

    try {
      const histJson = localStorage.getItem(
        "ingester-history"
      );
      const historyItems = histJson
        ? JSON.parse(histJson)
        : [];

      historyItems.forEach((item: { name: string }) => {
        selectedNames.add(item.name.toLowerCase());
      });
    } catch {
      // ignore invalid history cache
    }

    Array.from(selectedFiles).forEach((file) => {
      const normalizedName = file.name.toLowerCase();
      const duplicate =
        selectedNames.has(normalizedName);

      if (duplicate) {
        window.alert(
          `${file.name} has already been uploaded or selected.`
        );
        return;
      }

      selectedNames.add(normalizedName);

      const id =
        Date.now().toString() +
        Math.random().toString();

      filesRef.current[id] = file;

      const totalChunks = Math.ceil(
        file.size / CHUNK_SIZE
      );

      const f: UploadedFile = {
        id,
        name: file.name,
        size: formatFileSize(file.size),
        source: "Local",
        status: "Waiting",
        progress: 0,
        totalChunks,
      };

      uploadFileChunks(file, id);
      incoming.push(f);
    });

    if (incoming.length === 0) return;

    setFiles((prev) => [...prev, ...incoming]);
  };

  const renderContent = () => {
    if (activeMenu === "History") {
      return <History />;
    }

    if (activeMenu === "Profile") {
      return <Profile />;
    }

    return (
      <section className="home-content">
        <div className="welcome-section">
          <p className="welcome-label">
            INGESTER PLATFORM
          </p>

          <h1>
            Welcome back,
          </h1>

          <p className="welcome-description">
            Choose a data source to start ingestion
          </p>
        </div>

        <div className="source-section">
          <DataSourceSelector
            selectedSource={selectedSource}
            onSourceSelect={setSelectedSource}
          />

          {selectedSource === "Local Storage" && (
            <UploadBox
              onFilesSelected={handleFilesSelected}
            />
          )}

          <UploadProgress files={files} />
        </div>

        <div className="file-section">
          <div className="file-header">
            <div>
              <h2>Uploaded Files</h2>

              <p>
                Track your ingestion files
              </p>
            </div>

            <span className="file-count">
              {files.length} Files
            </span>
          </div>

          <FileTable files={files} />
        </div>
      </section>
    );
  };

  return (
    <div className="home-page">
      <Sidebar
        activeMenu={activeMenu}
        onMenuChange={setActiveMenu}
        onLogout={onLogout}
      />

      <main className="main-content">
      {/* <Header /> */}
        {renderContent()}
      </main>
    </div>
  );
};

export default Home;