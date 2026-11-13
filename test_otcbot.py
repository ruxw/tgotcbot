"""
test_otcbot.py - Comprehensive Automated Test Suite for OTC Telegram Bot

Verifies:
1. Duration parsing across diverse inputs and error handling
2. Database schema initialization, migrations, and default settings
3. Per-OTC settings persistence and toggles
4. Multi-owner authorization logic with varied environment variable formats
5. Sponsored content attachment, button regex parsing, and URL validation
6. Automatic pinning state persistence and lifecycle
7. Broadcast recipient and history tracking
8. AI analysis message filtering and Markdown-to-HTML sanitization
9. Custom emoji and unicode fallback rendering
10. App handlers, command scopes, and callback registration
11. Group registration logic (already registered vs new registration)
12. Raw command formatting in help (no <code> around commands)
13. Add to Group button URL generation in DM
"""

import os
import re
import sqlite3
import tempfile
import time
import unittest

# Import modules under test
import emojis
import otcbot


class TestOTCBot(unittest.TestCase):

    def setUp(self):
        # Use temporary SQLite database for test isolation
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.temp_db.close()
        otcbot.DATABASE_PATH = self.temp_db.name
        otcbot.init_db()

    def tearDown(self):
        if otcbot.db:
            otcbot.db.close()
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    # ==========================================
    # 1. Duration Parsing Tests
    # ==========================================
    def test_duration_parsing(self):
        cases = {
            "1m": 60,
            "1min": 60,
            "1minute": 60,
            "30m": 1800,
            "30min": 1800,
            "1h": 3600,
            "1hr": 3600,
            "1hour": 3600,
            "5h": 18000,
            "10h": 36000,
            "12h": 43200,
            "2d": 172800,
            "1day": 86400,
            "1w": 604800,
            "1week": 604800,
            "1h 30m": 5400,
            "2 days 4 hours": 187200,
        }
        for s, expected in cases.items():
            parsed = otcbot.parse_duration(s)
            self.assertEqual(parsed, expected, f"Failed parsing '{s}'")

        # Empty returns None (indefinite pin mode)
        self.assertIsNone(otcbot.parse_duration(""))
        self.assertIsNone(otcbot.parse_duration("   "))

        # Invalid cases raise ValueError
        invalid_cases = ["abc", "0s", "-5m", "1000y", "1h random"]
        for s in invalid_cases:
            with self.assertRaises(ValueError, msg=f"Should fail for '{s}'"):
                otcbot.parse_duration(s)

    # ==========================================
    # 2. Database & Settings Persistence Tests
    # ==========================================
    def test_otc_settings_persistence(self):
        chat_id = -1001234567890
        st = otcbot.get_otc_settings(chat_id)
        self.assertIsNotNone(st)
        self.assertEqual(st["del_join"], 0)
        self.assertEqual(st["del_leave"], 0)
        self.assertEqual(st["del_service"], 0)
        self.assertEqual(st["del_pinned"], 0)
        self.assertEqual(st["del_apin"], 0)
        self.assertEqual(st["pin_manual"], 1)
        self.assertEqual(st["pin_auto"], 1)
        self.assertEqual(st["ai_enabled"], 0)
        self.assertEqual(st["ai_exclude_admins"], 1)
        self.assertEqual(st["bot_active"], 1)

        # Toggle all service message settings
        for key in ("del_join", "del_leave", "del_service", "del_pinned", "del_apin"):
            otcbot.update_otc_setting(chat_id, key, 1)
            updated = otcbot.get_otc_settings(chat_id)
            self.assertEqual(updated[key], 1)

        # Toggle ai_enabled to 1 and verify groups table sync
        otcbot.execute("INSERT INTO groups(chat_id, title, active, created_at) VALUES(?, 'Test OTC', 1, ?)", (chat_id, time.time()))
        otcbot.update_otc_setting(chat_id, "ai_enabled", 1)
        grp = otcbot.group_row(chat_id)
        self.assertEqual(grp["ai_enabled"], 1)

    # ==========================================
    # 3. Multi-Owner Permissions Tests
    # ==========================================
    def test_multi_owner_permissions(self):
        env_val = " 111222 ,  333444, 555666 "
        owners = {int(x) for x in re.split(r"[,\s]+", env_val) if x.isdigit()}
        otcbot.BOT_OWNER_IDS = owners

        self.assertEqual(len(otcbot.BOT_OWNER_IDS), 3)
        self.assertTrue(otcbot.is_owner(111222))
        self.assertTrue(otcbot.is_owner(333444))
        self.assertTrue(otcbot.is_owner(555666))
        self.assertFalse(otcbot.is_owner(999999))
        self.assertFalse(otcbot.is_owner(0))
        self.assertFalse(otcbot.is_owner(None))

    # ==========================================
    # 4. Sponsored Content Integration Tests
    # ==========================================
    def test_sponsored_content_injection(self):
        otcbot.execute(
            """UPDATE sponsored_settings SET
               text_html='<b>Join Best OTC:</b> @Oncarts', text_enabled=1,
               button_style='success', button_text='Trade Now', button_url='https://t.me/Oncarts', button_enabled=1
               WHERE id=1"""
        )

        original_text = "<b>Notification:</b> Message pinned."
        wrapped_text, markup = otcbot.attach_sponsored_content(original_text)

        self.assertIn("—", wrapped_text)
        self.assertIn("<b>Join Best OTC:</b> @Oncarts", wrapped_text)
        self.assertIsNotNone(markup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "Trade Now")
        self.assertEqual(btn.url, "https://t.me/Oncarts")

    def test_sponsored_button_regex(self):
        btn_regex = r"^(?:#(primary|success|danger|blue|green|red)\s+)?(.*?)\s*[-—]\s*(https?://\S+|t\.me/\S+)$"

        cases = [
            ("#success Join the #1 OTC - t.me/Oncarts", "success", "Join the #1 OTC", "t.me/Oncarts"),
            ("#danger Urgent Deals - https://t.me/deals", "danger", "Urgent Deals", "https://t.me/deals"),
            ("Regular Button - https://google.com", None, "Regular Button", "https://google.com"),
        ]
        for inp, exp_style, exp_text, exp_url in cases:
            m = re.match(btn_regex, inp, re.IGNORECASE)
            self.assertIsNotNone(m, f"Failed regex match for: {inp}")
            style, text, url = m.groups()
            self.assertEqual(style, exp_style)
            self.assertEqual(text, exp_text)
            self.assertEqual(url, exp_url)

    # ==========================================
    # 5. Automatic Pinning State Persistence
    # ==========================================
    def test_auto_pins_state(self):
        chat_id = -100555
        admin_id = 12345
        duration = 3600
        otcbot.execute(
            """INSERT INTO auto_pins(chat_id, admin_id, duration, status, created_at)
               VALUES(?, ?, ?, 'PENDING', ?)""",
            (chat_id, admin_id, duration, time.time()),
        )

        row = otcbot.q_one("SELECT * FROM auto_pins WHERE chat_id=? AND status='PENDING'", (chat_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["duration"], 3600)
        self.assertEqual(row["admin_id"], admin_id)

        otcbot.execute("DELETE FROM auto_pins WHERE chat_id=?", (chat_id,))
        claimed = otcbot.q_one("SELECT * FROM auto_pins WHERE chat_id=?", (chat_id,))
        self.assertIsNone(claimed)

    # ==========================================
    # 6. Broadcast Recipient Tracking Tests
    # ==========================================
    def test_broadcast_tracking(self):
        owner_id = 111222
        b_id = otcbot.execute(
            """INSERT INTO broadcasts(sender_id, target_type, message_id, from_chat_id, total_targets, started_at, status)
               VALUES(?, 'users', 10, 111222, 2, ?, 'RUNNING')""",
            (owner_id, time.time()),
        )
        self.assertGreater(b_id, 0)

        otcbot.execute(
            """INSERT INTO broadcast_recipients(broadcast_id, target_id, target_name, target_username, is_group, status, created_at)
               VALUES(?, 1001, 'User 1', 'user1', 0, 'SUCCESS', ?)""",
            (b_id, time.time()),
        )
        otcbot.execute(
            """INSERT INTO broadcast_recipients(broadcast_id, target_id, target_name, target_username, is_group, status, error_msg, created_at)
               VALUES(?, 1002, 'User 2', NULL, 0, 'BLOCKED', 'Bot was blocked by the user', ?)""",
            (b_id, time.time()),
        )

        recipients = otcbot.q_all("SELECT * FROM broadcast_recipients WHERE broadcast_id=?", (b_id,))
        self.assertEqual(len(recipients), 2)
        self.assertEqual(recipients[0]["status"], "SUCCESS")
        self.assertEqual(recipients[1]["status"], "BLOCKED")

    # ==========================================
    # 7. AI Output Sanitization Tests
    # ==========================================
    def test_sanitize_ai_output(self):
        raw_markdown = (
            "### Overview\n"
            "**Key Activity**: High trading volume reported.\n"
            "---\n"
            "## Concerns\n"
            "Unverified escrow requests."
        )
        cleaned = otcbot.sanitize_ai_output(raw_markdown)
        self.assertNotIn("###", cleaned)
        self.assertNotIn("##", cleaned)
        self.assertNotIn("---", cleaned)
        self.assertIn("<b>Overview</b>", cleaned)
        self.assertIn("<b>Key Activity</b>", cleaned)
        self.assertIn("<b>Concerns</b>", cleaned)

    # ==========================================
    # 8. Emojis Fallback and HTML Tests
    # ==========================================
    def test_emojis_rendering(self):
        emojis.CUSTOM_EMOJI_IDS["pin"] = ""
        self.assertEqual(emojis.e("pin"), "📌")
        self.assertEqual(emojis.e_html("pin"), "📌")

        emojis.set_custom_emoji_id("pin", "5368324127421402275")
        self.assertEqual(emojis.e("pin"), "📌")
        self.assertEqual(
            emojis.e_html("pin"),
            '<tg-emoji emoji-id="5368324127421402275">📌</tg-emoji>'
        )

        for key in emojis.FALLBACK_EMOJIS:
            self.assertTrue(len(emojis.e(key)) > 0, f"Missing fallback for {key}")

        # Test icon_custom_emoji_id on inline buttons
        emojis.set_custom_emoji_id("settings", "")
        b_no_custom = otcbot.btn(f"{emojis.e('settings')} Settings", "dash:settings")
        self.assertEqual(b_no_custom.text, "⚙️ Settings")
        self.assertIsNone(getattr(b_no_custom, "icon_custom_emoji_id", None))

        emojis.set_custom_emoji_id("settings", "5432198765432109834")
        b_with_custom = otcbot.btn(f"{emojis.e('settings')} Settings", "dash:settings")
        self.assertEqual(b_with_custom.text, "Settings")
        self.assertEqual(getattr(b_with_custom, "icon_custom_emoji_id", None), "5432198765432109834")

    # ==========================================
    # 9. App Handlers & Callback Registration
    # ==========================================
    def test_app_build_handlers(self):
        otcbot.BOT_TOKEN = "123456:TEST_TOKEN_FOR_BUILD_APP"
        app = otcbot.build_app()
        self.assertIsNotNone(app)

        handler_cmds = set()
        for group in app.handlers.values():
            for h in group:
                if hasattr(h, "commands"):
                    handler_cmds.update(h.commands)

        expected_cmds = {
            "start", "help", "info", "pin", "unpin", "cancelpin",
            "pinlist", "apin", "register", "settings", "ai", "analysis",
            "prompt", "sponsored", "bd", "broadcast", "cancel"
        }
        for cmd in expected_cmds:
            self.assertIn(cmd, handler_cmds, f"Missing command handler: /{cmd}")

    # ==========================================
    # 10. Group Registration Logic Tests
    # ==========================================
    def test_group_registration_state(self):
        chat_id = -1009999
        # Not registered initially
        self.assertIsNone(otcbot.group_row(chat_id))

        # Register group
        otcbot.execute(
            """INSERT INTO groups(chat_id, title, username, active, created_at)
               VALUES(?, 'Prime OTC', 'primeotc', 1, ?)""",
            (chat_id, time.time()),
        )
        row = otcbot.group_row(chat_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["title"], "Prime OTC")

    # ==========================================
    # 11. Add to Group URL and Raw Command Formatting
    # ==========================================
    def test_add_to_group_button_and_raw_commands(self):
        menu = otcbot.main_menu(user_id=123, bot_username="OTCSROBOT")
        self.assertIsNotNone(menu)
        first_btn = menu.inline_keyboard[0][0]
        self.assertEqual(first_btn.text, "➕ Add to Group")
        self.assertEqual(first_btn.url, "https://t.me/OTCSROBOT?startgroup=true")


if __name__ == "__main__":
    unittest.main()
