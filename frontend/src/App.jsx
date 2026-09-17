/**
 * React chat UI for the RAG chatbot.
 * - Sidebar: list of chats (saved in localStorage)
 * - Main: messages + file attach + send
 * - Talks to Flask backend: POST /upload, POST /chat, DELETE /documents
 */
import { useEffect, useRef, useState } from 'react';
import './App.css';

// Key used to persist all chats in the browser
const STORAGE_KEY = 'rag-chats-v1';

/** Create an empty chat object (id is also used as backend chat_id). */
function newChat() {
  return {
    id: crypto.randomUUID(),
    title: 'New chat',
    messages: [],
    documents: [],
  };
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileCard({ fileName, fileType, fileSize, words, onRemove }) {
  const kind = fileType || ((fileName || '').toLowerCase().endsWith('.pdf') ? 'PDF' : 'TXT');
  return (
    <div className="file-card">
      <div className={`file-icon ${kind === 'PDF' ? 'pdf' : 'txt'}`}>{kind}</div>
      <div className="file-meta">
        <div className="file-name">{fileName}</div>
        <div className="file-sub">
          {[kind, formatSize(fileSize), words ? `${words} words` : ''].filter(Boolean).join(' · ')}
        </div>
      </div>
      {onRemove && (
        <button type="button" className="file-remove" onClick={onRemove} aria-label="Remove file">
          ×
        </button>
      )}
    </div>
  );
}

/** Load chats from localStorage, or start with one empty chat. */
function loadChats() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (Array.isArray(parsed) && parsed.length) {
      return parsed;
    }
  } catch {
    // ignore broken storage
  }
  return [newChat()];
}

function App() {
  const [chats, setChats] = useState(loadChats);
  const [activeId, setActiveId] = useState(chats[0].id);
  const [input, setInput] = useState('');
  const [pendingFile, setPendingFile] = useState(null); // file chosen but not uploaded yet
  const [loading, setLoading] = useState(false);         // waiting for /chat reply
  const [uploading, setUploading] = useState(false);     // waiting for /upload
  const [useWeb, setUseWeb] = useState(true);            // live website search for news/market
  const chatWindowRef = useRef(null);
  const fileRef = useRef(null);

  const activeChat = chats.find((chat) => chat.id === activeId) || chats[0];

  // Keep chats in localStorage whenever they change
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(chats));
  }, [chats]);

  // Auto-scroll to the latest message
  useEffect(() => {
    const el = chatWindowRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [activeChat?.messages, loading, uploading, activeId]);

  /** Update only the currently open chat in state. */
  const updateActiveChat = (updater) => {
    setChats((prev) => prev.map((chat) => (
      chat.id === activeId ? updater(chat) : chat
    )));
  };

  const startNewChat = () => {
    const chat = newChat();
    setChats((prev) => [chat, ...prev]);
    setActiveId(chat.id);
    setInput('');
    setPendingFile(null);
  };

  const selectChat = (chatId) => {
    setActiveId(chatId);
    setPendingFile(null);
  };

  const deleteChat = (event, chatId) => {
    event.stopPropagation();
    setChats((prev) => {
      const next = prev.filter((chat) => chat.id !== chatId);
      const remaining = next.length ? next : [newChat()];
      if (chatId === activeId) {
        setActiveId(remaining[0].id);
        setPendingFile(null);
      }
      return remaining;
    });
    // Also clear indexed documents for this chat on the backend
    fetch(`/documents?chat_id=${chatId}`, { method: 'DELETE' }).catch(() => {});
  };

  /** Remember the selected file until the user hits Send. */
  const handleFilePick = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setPendingFile({
      file,
      fileName: file.name,
      fileType: file.name.toLowerCase().endsWith('.pdf') ? 'PDF' : 'TXT',
      fileSize: file.size,
    });
    e.target.value = '';
  };

  /** Upload file to Flask -> extract text -> store in Chroma for this chat_id. */
  const uploadFile = async (attachment, chatId) => {
    const formData = new FormData();
    formData.append('file', attachment.file);
    formData.append('chat_id', chatId);
    const response = await fetch('/upload', {
      method: 'POST',
      body: formData,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || 'Upload failed.');
    }
    return data;
  };

  /**
   * Send flow:
   * 1) If a file is attached, upload it first
   * 2) Show the user message in the thread
   * 3) Call /chat and append the assistant reply (or error)
   */
  const sendMessage = async (e) => {
    e.preventDefault();
    if (loading || uploading || !activeChat) return;

    const text = input.trim();
    const attachment = pendingFile;
    if (!text && !attachment) return;

    // If user only attached a file, ask for a summary by default
    const question = text || 'Summarize this document.';
    setInput('');
    setPendingFile(null);
    setLoading(true);

    try {
      let documents = activeChat.documents;
      let uploadInfo = {};
      if (attachment) {
        setUploading(true);
        uploadInfo = await uploadFile(attachment, activeChat.id);
        documents = uploadInfo.documents || [uploadInfo.name];
        setUploading(false);
      }

      // First message sets the sidebar title
      const title = activeChat.title === 'New chat'
        ? (text || attachment.fileName).slice(0, 36)
        : activeChat.title;

      updateActiveChat((chat) => ({
        ...chat,
        title,
        documents,
        messages: [
          ...chat.messages,
          {
            role: 'user',
            content: question,
            file: attachment
              ? {
                  fileName: attachment.fileName,
                  fileType: attachment.fileType,
                  fileSize: attachment.fileSize,
                  method: uploadInfo.method,
                  words: uploadInfo.words,
                }
              : undefined,
          },
        ],
      }));

      // Ask the backend (RAG and/or live web search + LLM) for an answer
      const response = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: question,
          chat_id: activeChat.id,
          use_web: useWeb && !documents?.length,
        }),
      });
      const data = await response.json().catch(() => ({}));

      if (response.ok && data.reply) {
        updateActiveChat((chat) => ({
          ...chat,
          messages: [
            ...chat.messages,
            {
              role: 'assistant',
              content: data.reply,
              sources: data.sources || [],
              mode: data.mode,
            },
          ],
        }));
      } else {
        const errorMessage = typeof data.error === 'string'
          ? data.error
          : data.error?.message || 'Something went wrong.';
        updateActiveChat((chat) => ({
          ...chat,
          messages: [...chat.messages, { role: 'assistant', content: `Error: ${errorMessage}` }],
        }));
      }
    } catch (error) {
      setUploading(false);
      updateActiveChat((chat) => ({
        ...chat,
        messages: [
          ...chat.messages,
          { role: 'assistant', content: `Error: ${error.message || 'Error connecting to backend.'}` },
        ],
      }));
    } finally {
      setLoading(false);
      setUploading(false);
    }
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-title">Chatbot</div>
        <button type="button" className="new-chat-btn" onClick={startNewChat}>
          + New chat
        </button>
        <p className="history-label">Chats</p>
        <div className="chat-history">
          {chats.map((chat) => (
            <div
              key={chat.id}
              className={`history-item ${chat.id === activeId ? 'active' : ''}`}
              onClick={() => selectChat(chat.id)}
            >
              <span>{chat.title}</span>
              <button type="button" className="delete-chat" onClick={(event) => deleteChat(event, chat.id)}>
                ×
              </button>
            </div>
          ))}
          {!chats.length && <p className="history-empty">No chats yet</p>}
        </div>
      </aside>

      <main className="main">
        <div className="main-header">{activeChat?.title || 'New chat'}</div>
        <div className="chat-window" ref={chatWindowRef}>
          {!activeChat?.messages.length && !loading && (
            <div className="empty-state">
              <h2>What can I help with?</h2>
              <p>Ask current affairs with Search web on, or attach a file with +.</p>
            </div>
          )}
          <div className="chat-thread">
            {activeChat?.messages.map((msg, index) => (
              <div key={index} className={`chat-row ${msg.role}`}>
                {msg.file && (
                  <FileCard
                    fileName={msg.file.fileName}
                    fileType={msg.file.fileType}
                    fileSize={msg.file.fileSize}
                    words={msg.file.words}
                  />
                )}
                {msg.type === 'file' && !msg.file && (
                  <FileCard
                    fileName={msg.fileName}
                    fileType={msg.fileType}
                    fileSize={msg.fileSize}
                  />
                )}
                {msg.content && (
                  <>
                    <span className="chat-bubble">{msg.content}</span>
                    {msg.role === 'assistant' && msg.mode === 'web' && (
                      <div className="chat-mode">Answered from live web search</div>
                    )}
                    {msg.role === 'assistant' && msg.sources?.length > 0 && (
                      <div className="chat-sources">
                        Sources:{' '}
                        {msg.sources.map((source, sourceIndex) => {
                          const isUrl = /^https?:\/\//i.test(source);
                          return (
                            <span key={`${source}-${sourceIndex}`}>
                              {sourceIndex > 0 ? ', ' : ''}
                              {isUrl ? (
                                <a href={source} target="_blank" rel="noreferrer">
                                  {source}
                                </a>
                              ) : (
                                source
                              )}
                            </span>
                          );
                        })}
                      </div>
                    )}
                  </>
                )}
              </div>
            ))}
            {uploading && <p className="chat-status">Uploading document...</p>}
            {loading && !uploading && (
              <p className="chat-status">
                {useWeb && !activeChat?.documents?.length
                  ? 'Searching the web...'
                  : 'Thinking...'}
              </p>
            )}
          </div>
        </div>

        <div className="composer-wrap">
          <div className="composer">
            <form onSubmit={sendMessage} className="composer-box">
              {pendingFile && (
                <div className="composer-attach">
                  <FileCard
                    fileName={pendingFile.fileName}
                    fileType={pendingFile.fileType}
                    fileSize={pendingFile.fileSize}
                    onRemove={() => setPendingFile(null)}
                  />
                </div>
              )}
              <div className="composer-options">
                <label className="web-toggle" title="Search websites for news and market questions">
                  <input
                    type="checkbox"
                    checked={useWeb}
                    onChange={(e) => setUseWeb(e.target.checked)}
                    disabled={loading || uploading}
                  />
                  Search web
                </label>
              </div>
              <div className="chat-form">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".txt,.pdf,text/plain,application/pdf"
                  hidden
                  onChange={handleFilePick}
                />
                <button
                  type="button"
                  className="icon-btn"
                  title="Attach document"
                  disabled={uploading || loading}
                  onClick={() => fileRef.current?.click()}
                >
                  +
                </button>
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={
                    pendingFile
                      ? 'Ask about this file...'
                      : useWeb
                        ? 'Ask current affairs or market news...'
                        : 'Ask anything'
                  }
                  disabled={loading}
                />
                <button
                  type="submit"
                  className="send-btn"
                  disabled={loading || uploading || (!input.trim() && !pendingFile)}
                >
                  Send
                </button>
              </div>
            </form>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
