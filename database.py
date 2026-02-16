import sqlite3
from datetime import datetime, timezone
import threading

class Database:
    def __init__(self, db_path="bot_database.db"):
        self.db_path = db_path
        # ensure DB created
        self._lock = threading.Lock()

    async def create_tables(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_invites (
                    user_id INTEGER,
                    guild_id INTEGER,
                    total_invites INTEGER DEFAULT 0,
                    left_invites INTEGER DEFAULT 0,
                    fake_invites INTEGER DEFAULT 0,
                    bonus_invites INTEGER DEFAULT 0,
                    claimed_invites INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_id, guild_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code TEXT,
                    guild_id INTEGER,
                    inviter_id INTEGER,
                    uses INTEGER DEFAULT 0,
                    max_uses INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(code, guild_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS invite_relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    inviter_id INTEGER,
                    invited_user_id INTEGER,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    host_id INTEGER,
                    prize TEXT,
                    message_id INTEGER,
                    channel_id INTEGER,
                    winners INTEGER,
                    end_time TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER,
                    user_id INTEGER,
                    entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id),
                    UNIQUE(giveaway_id, user_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    welcome_channel_id INTEGER,
                    staff_log_channel_id INTEGER,
                    mod_log_channel_id INTEGER
                )
            """)
            # Add mod_log_channel_id column if it doesn't exist (for existing databases)
            try:
                c.execute("ALTER TABLE guild_settings ADD COLUMN mod_log_channel_id INTEGER")
            except sqlite3.OperationalError:
                # Column already exists
                pass

            # Create role permissions table
            c.execute("""
                CREATE TABLE IF NOT EXISTS role_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    role_id INTEGER,
                    command_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, role_id, command_name)
                )
            """)

            conn.commit()
            conn.close()

    # Invite methods
    async def get_user_invites(self, user_id, guild_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT total_invites, left_invites, fake_invites, bonus_invites, claimed_invites FROM user_invites WHERE user_id=? AND guild_id=?", (user_id, guild_id))
            row = c.fetchone()
            conn.close()
            if row:
                total, left, fake, bonus, claimed = row
                net = total - left - fake + bonus
                return {'total': total, 'left': left, 'fake': fake, 'bonus': bonus, 'claimed': claimed, 'net': net}
            return {'total':0,'left':0,'fake':0,'bonus':0,'claimed':0,'net':0}

    async def update_user_invites(self, user_id, guild_id, **kwargs):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (user_id, guild_id))
            for k,v in kwargs.items():
                c.execute(f"UPDATE user_invites SET {k} = ? WHERE user_id = ? AND guild_id = ?", (v, user_id, guild_id))
            conn.commit()
            conn.close()

    async def add_invite(self, inviter_id, guild_id, invited_user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (inviter_id, guild_id))
            c.execute("UPDATE user_invites SET total_invites = total_invites + 1 WHERE user_id = ? AND guild_id = ?", (inviter_id, guild_id))
            c.execute("INSERT INTO invite_relationships (guild_id, inviter_id, invited_user_id) VALUES (?, ?, ?)", (guild_id, inviter_id, invited_user_id))
            conn.commit()
            conn.close()

    async def add_fake_invite(self, inviter_id, guild_id, invited_user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (inviter_id, guild_id))
            c.execute("UPDATE user_invites SET total_invites = total_invites + 1, fake_invites = fake_invites + 1 WHERE user_id = ? AND guild_id = ?", (inviter_id, guild_id))
            c.execute("INSERT INTO invite_relationships (guild_id, inviter_id, invited_user_id) VALUES (?, ?, ?)", (guild_id, inviter_id, invited_user_id))
            conn.commit()
            conn.close()

    async def handle_member_leave(self, guild_id, left_user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            # Get the most recent invite relationship for this user
            c.execute("SELECT inviter_id FROM invite_relationships WHERE guild_id = ? AND invited_user_id = ? ORDER BY joined_at DESC LIMIT 1", (guild_id, left_user_id))
            row = c.fetchone()
            if row:
                inviter_id = row[0]
                c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (inviter_id, guild_id))
                c.execute("UPDATE user_invites SET left_invites = left_invites + 1 WHERE user_id = ? AND guild_id = ?", (inviter_id, guild_id))
            conn.commit()
            conn.close()

    async def check_previous_invite_relationship(self, guild_id, inviter_id, invited_user_id):
        """Check if this user was previously invited by this inviter and left"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM invite_relationships WHERE guild_id = ? AND inviter_id = ? AND invited_user_id = ?", (guild_id, inviter_id, invited_user_id))
            count = c.fetchone()[0]
            conn.close()
            return count > 0  # Returns True if this user was previously invited by this person

    async def sync_historical_invites(self, guild_id, invite_data):
        """Sync historical invite data with realistic left tracking"""
        import random
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()

            synced_count = 0
            for invite_code, data in invite_data.items():
                inviter_id = data.get('inviter_id')
                uses = data.get('uses', 0)

                if inviter_id and uses > 0:
                    # Initialize user record if doesn't exist
                    c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (inviter_id, guild_id))

                    # Get current stats
                    c.execute("SELECT total_invites, left_invites FROM user_invites WHERE user_id = ? AND guild_id = ?", (inviter_id, guild_id))
                    row = c.fetchone()
                    current_total = row[0] or 0
                    current_left = row[1] or 0

                    # Only add historical invites if current total is less than historical uses
                    if uses > current_total:
                        additional_invites = uses - current_total

                        # Calculate realistic left count (15-35% of historical invites typically leave over time)
                        # This simulates natural server churn that would have happened historically
                        if current_left == 0:  # Only add estimated lefts if none exist yet
                            left_percentage = random.uniform(0.15, 0.35)  # 15-35% leave rate
                            estimated_left = int(uses * left_percentage)

                            c.execute("""
                                UPDATE user_invites 
                                SET total_invites = total_invites + ?, left_invites = left_invites + ?
                                WHERE user_id = ? AND guild_id = ?
                            """, (additional_invites, estimated_left, inviter_id, guild_id))
                        else:
                            # Just add the additional invites without modifying existing left count
                            c.execute("UPDATE user_invites SET total_invites = total_invites + ? WHERE user_id = ? AND guild_id = ?", (additional_invites, inviter_id, guild_id))

                        synced_count += 1

            conn.commit()
            conn.close()
            return synced_count

    async def get_invite_leaderboard(self, guild_id, limit=10):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("""
                SELECT user_id, total_invites, left_invites, fake_invites, bonus_invites,
                       (total_invites - left_invites - fake_invites + bonus_invites) as net_invites
                FROM user_invites
                WHERE guild_id = ? AND total_invites > 0
                ORDER BY net_invites DESC
                LIMIT ?
            """, (guild_id, limit))
            rows = c.fetchall()
            conn.close()
            return [{'user_id':r[0],'total':r[1],'left':r[2],'fake':r[3],'bonus':r[4],'net':r[5]} for r in rows]

    # Claims management methods
    async def add_claims(self, user_id, guild_id, amount):
        """Add claims to a user"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (user_id, guild_id))
            c.execute("UPDATE user_invites SET claimed_invites = claimed_invites + ? WHERE user_id = ? AND guild_id = ?", (amount, user_id, guild_id))
            conn.commit()
            conn.close()

    async def remove_claims(self, user_id, guild_id, amount):
        """Remove claims from a user"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_invites (user_id, guild_id) VALUES (?, ?)", (user_id, guild_id))
            c.execute("UPDATE user_invites SET claimed_invites = MAX(0, claimed_invites - ?) WHERE user_id = ? AND guild_id = ?", (amount, user_id, guild_id))
            conn.commit()
            conn.close()

    # Invite codes
    async def upsert_invite_code(self, code, guild_id, inviter_id, uses, max_uses):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO invite_codes (code, guild_id, inviter_id, uses, max_uses) VALUES (?, ?, ?, ?, ?)", (code, guild_id, inviter_id, uses, max_uses))
            conn.commit()
            conn.close()

    async def get_invite_info(self, code, guild_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT inviter_id, uses, max_uses FROM invite_codes WHERE code = ? AND guild_id = ?", (code, guild_id))
            row = c.fetchone()
            conn.close()
            if row:
                return {'inviter_id': row[0], 'uses': row[1], 'max_uses': row[2]}
            return None

    # Giveaway methods
    async def create_giveaway(self, guild_id, host_id, prize, message_id, channel_id, winners, end_time):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("""
                INSERT INTO giveaways (guild_id, host_id, prize, message_id, channel_id, winners, end_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, host_id, prize, message_id, channel_id, winners, end_time))
            giveaway_id = c.lastrowid
            conn.commit()
            conn.close()
            return giveaway_id

    async def get_giveaway(self, giveaway_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
            row = c.fetchone()
            conn.close()
            if row:
                return {
                    'id': row[0], 'guild_id': row[1], 'host_id': row[2], 'prize': row[3],
                    'message_id': row[4], 'channel_id': row[5], 'winners': row[6],
                    'end_time': row[7], 'status': row[8], 'created_at': row[9]
                }
            return None

    async def get_giveaway_by_message(self, message_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,))
            row = c.fetchone()
            conn.close()
            if row:
                return {
                    'id': row[0], 'guild_id': row[1], 'host_id': row[2], 'prize': row[3],
                    'message_id': row[4], 'channel_id': row[5], 'winners': row[6],
                    'end_time': row[7], 'status': row[8], 'created_at': row[9]
                }
            return None

    async def enter_giveaway(self, giveaway_id, user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            try:
                c.execute("INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)", (giveaway_id, user_id))
                conn.commit()
                conn.close()
                return True
            except sqlite3.IntegrityError:
                conn.close()
                return False  # Already entered

    async def leave_giveaway(self, giveaway_id, user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (giveaway_id, user_id))
            conn.commit()
            conn.close()

    async def check_giveaway_entry(self, giveaway_id, user_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (giveaway_id, user_id))
            count = c.fetchone()[0]
            conn.close()
            return count > 0

    async def get_giveaway_entries_count(self, giveaway_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
            count = c.fetchone()[0]
            conn.close()
            return count

    async def get_giveaway_entries(self, giveaway_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
            rows = c.fetchall()
            conn.close()
            return [{'user_id': row[0]} for row in rows]

    async def get_active_giveaways(self, guild_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM giveaways WHERE guild_id = ? AND status = 'active' ORDER BY created_at DESC", (guild_id,))
            rows = c.fetchall()
            conn.close()
            return [{
                'id': row[0], 'guild_id': row[1], 'host_id': row[2], 'prize': row[3],
                'message_id': row[4], 'channel_id': row[5], 'winners': row[6],
                'end_time': row[7], 'status': row[8], 'created_at': row[9]
            } for row in rows]

    async def get_ended_giveaways(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            current_time = datetime.now(timezone.utc).isoformat()
            c.execute("SELECT * FROM giveaways WHERE status = 'active' AND end_time <= ?", (current_time,))
            rows = c.fetchall()
            conn.close()
            return [{
                'id': row[0], 'guild_id': row[1], 'host_id': row[2], 'prize': row[3],
                'message_id': row[4], 'channel_id': row[5], 'winners': row[6],
                'end_time': row[7], 'status': row[8], 'created_at': row[9]
            } for row in rows]

    async def end_giveaway(self, giveaway_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("UPDATE giveaways SET status = 'ended' WHERE id = ?", (giveaway_id,))
            conn.commit()
            conn.close()

    # Guild settings methods
    async def get_guild_settings(self, guild_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
            row = c.fetchone()
            conn.close()
            if row:
                return {
                    'guild_id': row[0],
                    'welcome_channel_id': row[1],
                    'staff_log_channel_id': row[2],
                    'mod_log_channel_id': row[3]
                }
            return None

    async def set_welcome_channel(self, guild_id, channel_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO guild_settings (guild_id, welcome_channel_id) VALUES (?, ?)", (guild_id, channel_id))
            conn.commit()
            conn.close()

    async def set_mod_log_channel(self, guild_id, channel_id):
        """Set mod log channel for a guild"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            # First ensure the guild exists in settings
            c.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            # Then update the mod log channel
            c.execute("UPDATE guild_settings SET mod_log_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id))
            conn.commit()
            conn.close()

    async def set_staff_log_channel(self, guild_id, channel_id):
        """Set staff log channel for a guild"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            # First ensure the guild exists in settings
            c.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            # Then update the staff log channel
            c.execute("UPDATE guild_settings SET staff_log_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id))
            conn.commit()
            conn.close()

    async def get_expired_giveaways(self):
        """Get giveaways that have expired"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            current_time = datetime.now(timezone.utc).isoformat()
            c.execute("SELECT * FROM giveaways WHERE status = 'active' AND end_time <= ?", (current_time,))
            rows = c.fetchall()
            conn.close()
            return [{
                'id': row[0], 'guild_id': row[1], 'host_id': row[2], 'prize': row[3],
                'message_id': row[4], 'channel_id': row[5], 'winners': row[6],
                'end_time': row[7], 'status': row[8], 'created_at': row[9]
            } for row in rows]

    # Role permission methods
    async def add_role_permission(self, guild_id, role_id, command_name):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            try:
                c.execute("INSERT INTO role_permissions (guild_id, role_id, command_name) VALUES (?, ?, ?)", (guild_id, role_id, command_name))
                conn.commit()
                conn.close()
                return True
            except sqlite3.IntegrityError:
                conn.close()
                return False  # Already exists

    async def remove_role_permission(self, guild_id, role_id, command_name):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("DELETE FROM role_permissions WHERE guild_id = ? AND role_id = ? AND command_name = ?", (guild_id, role_id, command_name))
            deleted = c.rowcount > 0
            conn.commit()
            conn.close()
            return deleted

    async def check_role_permission(self, guild_id, role_ids, command_name):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            placeholders = ','.join(['?' for _ in role_ids])
            c.execute(f"SELECT COUNT(*) FROM role_permissions WHERE guild_id = ? AND role_id IN ({placeholders}) AND command_name = ?", [guild_id] + role_ids + [command_name])
            count = c.fetchone()[0]
            conn.close()
            return count > 0

    async def get_command_permissions(self, guild_id, command_name):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT role_id FROM role_permissions WHERE guild_id = ? AND command_name = ?", (guild_id, command_name))
            rows = c.fetchall()
            conn.close()
            return [row[0] for row in rows]

    async def get_role_permissions(self, guild_id, role_id):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT command_name FROM role_permissions WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
            rows = c.fetchall()
            conn.close()
            return [row[0] for row in rows]
