import { AlertTriangle, Download } from "lucide-react";

import type { EndToEndVideoResult } from "./types";

function EndToEndVideoResultPanel({
  result,
}: {
  result: EndToEndVideoResult | null;
}) {
  if (!result) {
    return null;
  }
  const files = Object.entries(result["导出文件"] || {});
  return (
    <section className="export-file-list" aria-label="一分钟测试成片">
      <h2>一分钟测试成片</h2>
      {result["选题"] && (
        <div className="editing-status-panel">
          <div>
            <span>测试选题</span>
            <strong>{result["选题"]}</strong>
            <p>
              {result["画面数量"] || 0} 个画面｜
              {result["成功"] ? "已生成 MP4" : result["提示"] || "未生成"}
            </p>
          </div>
        </div>
      )}
      {files.length > 0 && (
        <div className="export-file-list compact-list">
          {files.map(([label, path]) => (
            <div className="export-file-row" key={label}>
              <Download size={16} aria-hidden="true" />
              <span>{label}</span>
              <code>{path}</code>
            </div>
          ))}
        </div>
      )}
      {result["提醒"] && result["提醒"].length > 0 && (
        <div className="message message-warning">
          <AlertTriangle size={17} aria-hidden="true" />
          <span>{result["提醒"].join("；")}</span>
        </div>
      )}
      <div className="timeline-table">
        <h3>素材匹配</h3>
        {result["素材匹配"].length === 0 ? (
          <div className="empty-state">还没有素材匹配记录。</div>
        ) : (
          result["素材匹配"].map((match) => (
            <article className="timeline-row" key={match["镜头"]}>
              <span>{match["时间段"]}</span>
              <div>
                <strong>
                  {match["素材状态"]}｜{match["素材类型"]}
                </strong>
                <p>{match["画面说明"]}</p>
                <small>{match["匹配原因"]}</small>
                {match["素材路径"] && <code>{match["素材路径"]}</code>}
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}

export default EndToEndVideoResultPanel;
