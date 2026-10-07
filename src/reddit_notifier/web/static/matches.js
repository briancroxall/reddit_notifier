// When you open an unread match's post (it opens in a new tab), update this
// page right away instead of waiting for a refresh. The new tab's request is
// what actually marks the post read on the server.

function markPostReadOnPage(postId) {
  const rows = document.querySelectorAll(`tr.unread[data-post-id="${CSS.escape(postId)}"]`);
  if (rows.length === 0) return;

  rows.forEach((row) => {
    row.classList.remove("unread");
    row.querySelectorAll(".badge-new, .actions-cell form").forEach((el) => el.remove());
  });

  // Counts are per post, so one post read = one fewer unread.
  const header = document.querySelector(".section-header");
  const remaining = Math.max(0, Number(header.dataset.unread) - 1);
  header.dataset.unread = remaining;

  document.getElementById("nav-unread").textContent = remaining ? ` (${remaining})` : "";
  const badge = document.getElementById("unread-badge");
  if (badge) {
    if (remaining) badge.textContent = `${remaining} new`;
    else badge.remove();
  }
  if (!remaining) document.getElementById("mark-all-form")?.remove();
}

function handleOpen(event) {
  const link = event.target.closest("a.post-link");
  if (!link) return;
  // "click" covers ordinary and Cmd-clicks; "auxclick" covers middle-clicks.
  if (event.type === "auxclick" && event.button !== 1) return;
  markPostReadOnPage(link.closest("tr").dataset.postId);
}

document.addEventListener("click", handleOpen);
document.addEventListener("auxclick", handleOpen);
