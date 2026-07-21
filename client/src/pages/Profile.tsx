import { useEffect, useState } from "react";
import "./Profile.css";

type Account = {
  username: string;
  email: string;
  createdAt?: string;
};

const Profile = () => {
  const [account, setAccount] = useState<Account | null>(null);

  useEffect(() => {
    try {
      const current = localStorage.getItem("ingester-current-user");
      const accountsJson = localStorage.getItem("ingester-accounts");
      const accounts = accountsJson ? JSON.parse(accountsJson) : [];

      if (current) {
        const matched = accounts.find(
          (a: Account & { email?: string }) =>
            a.email === current
        );

        if (matched) setAccount(matched);
      }
    } catch {
      setAccount(null);
    }
  }, []);

  if (!account) {
    return (
      <div className="profile-page">
        <h2>Profile</h2>
        <p>No signed-in user.</p>
      </div>
    );
  }

  return (
    <div className="profile-page">
      <h2>Profile</h2>

      <div className="profile-card">
        <div className="profile-row"><strong>Name:</strong> {account.username}</div>
        <div className="profile-row"><strong>Email:</strong> {account.email}</div>
        <div className="profile-row"><strong>Created:</strong> {account.createdAt ? new Date(account.createdAt).toLocaleString() : '-'}</div>
      </div>
    </div>
  );
};

export default Profile;
