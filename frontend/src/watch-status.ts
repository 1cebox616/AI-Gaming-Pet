const WATCH_STATUS_STYLE = `
  .watch-status {
    position: absolute;
    right: 22px;
    bottom: 184px;
    max-width: 260px;
    padding: 6px 10px;
    color: #ffffff;
    background: rgb(15 23 42 / 86%);
    border: 1px solid rgb(121 217 239 / 88%);
    border-radius: 999px;
    box-shadow: 0 3px 9px rgb(0 0 0 / 30%);
    font-family: "Microsoft YaHei UI", sans-serif;
    font-size: 12px;
    font-weight: 700;
    opacity: 0;
    overflow: hidden;
    pointer-events: none;
    text-overflow: ellipsis;
    transition: opacity 180ms ease;
    user-select: none;
    visibility: hidden;
    white-space: nowrap;
  }

  .watch-status.is-visible {
    opacity: 1;
    visibility: visible;
  }
`;

const host = document.getElementById("app");

if (!(host instanceof HTMLDivElement)) {
  throw new Error("watch status host is unavailable");
}

const style = document.createElement("style");
style.textContent = WATCH_STATUS_STYLE;
document.head.append(style);

const status = document.createElement("div");
status.className = "watch-status";
status.setAttribute("role", "status");
host.append(status);

export function setWatchStatus(game: string | null): void {
  if (game === null || game.trim().length === 0) {
    status.textContent = "";
    status.classList.remove("is-visible");
    return;
  }
  status.textContent = `正在观看：${game}`;
  status.classList.add("is-visible");
}
