# ── Vault CLI ─────────────────────────────────────────────────────────────────
echo ""
echo "[*] Installing Vault CLI..."
wget -O - https://apt.releases.hashicorp.com/gpg | \
    sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
    https://apt.releases.hashicorp.com $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/hashicorp.list > /dev/null

sudo apt update
sudo apt install -y vault
echo "[✓] Vault CLI installed"

echo -n "vault          : "; vault version
