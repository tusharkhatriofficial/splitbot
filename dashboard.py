import sqlite3
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Flat expenses", layout="wide")
df = pd.read_sql("SELECT * FROM expenses WHERE deleted=0", sqlite3.connect("expenses.db"))
st.title("Flat expenses")

if df.empty:
    st.info("No expenses yet. Send one in the Telegram group.")
    st.stop()

df["amount"] = df.amount_paise / 100
df["created_at"] = pd.to_datetime(df.created_at)
df["month"] = df.created_at.dt.to_period("M").astype(str)

months = ["All"] + sorted(df.month.unique(), reverse=True)
sel = st.selectbox("Month", months)
if sel != "All":
    df = df[df.month == sel]

c1, c2, c3 = st.columns(3)
c1.metric("Total spent", f"₹{df.amount.sum():,.0f}")
c2.metric("Entries", len(df))
c3.metric("Top category", df.groupby("category").amount.sum().idxmax())

a, b = st.columns(2)
a.subheader("By category")
a.bar_chart(df.groupby("category").amount.sum())
b.subheader("Paid by person")
b.bar_chart(df.groupby("payer").amount.sum())

st.subheader("Daily spending")
st.line_chart(df.set_index("created_at").amount.resample("D").sum())

st.subheader("History")
st.dataframe(df.sort_values("created_at", ascending=False)[
    ["id", "created_at", "payer", "amount", "category", "note"]], use_container_width=True)