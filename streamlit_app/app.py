import streamlit as st

from auth import login_gate, current_user, logout

st.set_page_config(page_title="Outreach Control Panel", page_icon="📬", layout="wide")

if not login_gate():
    st.stop()

with st.sidebar:
    st.success(f"Logged in as **{current_user()}**")
    if st.button("Log out"):
        logout()
        st.rerun()

st.title("📬 Outreach Control Panel")
st.markdown(
    """
Welcome. This dashboard is a **control surface**, not a second sending
system — every Preview, Send, and Check Replies action here either runs
the exact same `outreach.py` logic directly (Preview) or triggers the same
GitHub Actions workflows you'd run manually (Send, Check Replies). Nothing
here bypasses the safety checks already built into the repo: duplicate
protection, per-account capacity, header-verified reply matching, and the
typed `SEND` confirmation gate.

Use the sidebar to navigate:

- **📊 Dashboard** — read-only view of every campaign's leads, sends,
  replies, and errors.
- **🚀 Controls** — Preview a batch instantly, or trigger a real Send /
  Check Replies run and watch its status.
- **➕ New Campaign** — create a new campaign's Intro templates and open a
  pull request for review — nothing goes live until it's merged.
    """
)
