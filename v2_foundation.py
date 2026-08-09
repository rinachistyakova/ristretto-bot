"""
Random Ristretto v2 — foundation schema.

This module ONLY prepares new database fields and tables.
It does not change the current user-facing behaviour of the bot.
All migrations are additive and safe to run repeatedly.
"""


async def ensure_v2_foundation(conn, owner_id: int) -> None:
    # ---------------------------------------------------------
    # USERS: new profile, access and delivery fields
    # ---------------------------------------------------------
    user_columns = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS team TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS responsibility_text TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_step TEXT DEFAULT 'completed'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_migration_required INT DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_denied_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_denied_reason TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_interaction_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS delivery_available INT DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_delivery_error_at TIMESTAMPTZ",
    ]
    for sql in user_columns:
        await conn.execute(sql)

    # Existing users stay usable exactly as before.
    # The only status we can identify with certainty from the old schema
    # is a profile deleted by the user/admin.
    await conn.execute("""
        UPDATE users
        SET status = CASE
            WHEN deleted_at IS NOT NULL THEN 'self_deleted'
            ELSE COALESCE(NULLIF(status, ''), 'active')
        END
    """)

    await conn.execute("""
        UPDATE users
        SET role = 'user'
        WHERE role IS NULL OR TRIM(role) = ''
    """)

    # Keep the current technical administrator as the initial owner.
    await conn.execute("""
        UPDATE users
        SET role = 'owner'
        WHERE user_id = $1
    """, owner_id)

    # Existing users need to fill the new profile fields later,
    # but this flag is not used by the old UX yet.
    await conn.execute("""
        UPDATE users
        SET profile_migration_required = 1
        WHERE team IS NULL
           OR responsibility_text IS NULL
           OR TRIM(COALESCE(responsibility_text, '')) = ''
    """)

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_v2_status ON users(status)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_v2_role ON users(role)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_v2_team ON users(team)"
    )

    # ---------------------------------------------------------
    # WEEKLY PARTICIPATION
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_participation (
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            cycle_key TEXT NOT NULL,
            invited_at TIMESTAMPTZ,
            response TEXT,
            responded_at TIMESTAMPTZ,
            PRIMARY KEY (user_id, cycle_key)
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_participation_cycle
        ON weekly_participation(cycle_key)
    """)

    # ---------------------------------------------------------
    # NORMALIZED PAIRWISE MATCH HISTORY
    # A trio will later create three pairwise relationship rows.
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS match_relationships (
            id BIGSERIAL PRIMARY KEY,
            cycle_key TEXT NOT NULL,
            user_a BIGINT NOT NULL REFERENCES users(user_id),
            user_b BIGINT NOT NULL REFERENCES users(user_id),
            group_id BIGINT REFERENCES match_groups(id) ON DELETE SET NULL,
            matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (user_a <> user_b),
            UNIQUE (cycle_key, user_a, user_b)
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_match_relationships_users
        ON match_relationships(user_a, user_b)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_match_relationships_cycle
        ON match_relationships(cycle_key)
    """)

    # ---------------------------------------------------------
    # THANK-YOU BLOCKS
    # One row = one block period. Unblocking closes the row.
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS thanks_blocks (
            id BIGSERIAL PRIMARY KEY,
            blocker_id BIGINT NOT NULL REFERENCES users(user_id),
            blocked_sender_id BIGINT NOT NULL REFERENCES users(user_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            unblocked_at TIMESTAMPTZ,
            CHECK (blocker_id <> blocked_sender_id)
        )
    """)
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_thanks_block
        ON thanks_blocks(blocker_id, blocked_sender_id)
        WHERE unblocked_at IS NULL
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_thanks_blocks_sender
        ON thanks_blocks(blocked_sender_id)
    """)

    # ---------------------------------------------------------
    # THANKS: split private delivery from public digest state
    # ---------------------------------------------------------
    thanks_columns = [
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS public_text TEXT",
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS personal_status TEXT DEFAULT 'pending'",
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS public_status TEXT DEFAULT 'not_requested'",
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS moderated_by BIGINT",
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS moderated_at TIMESTAMPTZ",
        "ALTER TABLE thanks ADD COLUMN IF NOT EXISTS moderation_reason TEXT",
    ]
    for sql in thanks_columns:
        await conn.execute(sql)

    await conn.execute("""
        UPDATE thanks
        SET personal_status = CASE
            WHEN personal_delivered_at IS NOT NULL THEN 'delivered'
            ELSE 'pending'
        END
        WHERE personal_status IS NULL
           OR personal_status = ''
           OR personal_status = 'pending'
    """)

    await conn.execute("""
        UPDATE thanks
        SET public_status = CASE
            WHEN include_in_digest = 0 THEN 'not_requested'
            WHEN digest_delivered_at IS NOT NULL THEN 'published'
            ELSE 'pending'
        END
        WHERE public_status IS NULL
           OR public_status = ''
           OR public_status IN ('not_requested', 'pending')
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_thanks_public_status
        ON thanks(cycle_key, public_status)
    """)

    # ---------------------------------------------------------
    # PUBLIC DIGEST RUN / APPROVAL
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS digest_runs (
            cycle_key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'draft',
            approved_by BIGINT REFERENCES users(user_id),
            approved_at TIMESTAMPTZ,
            published_at TIMESTAMPTZ,
            returned_to_moderation_by BIGINT REFERENCES users(user_id),
            returned_to_moderation_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Part-level delivery prevents duplicated first parts of a long digest.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS digest_deliveries (
            cycle_key TEXT NOT NULL,
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            part_no INT NOT NULL,
            delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (cycle_key, user_id, part_no)
        )
    """)

    # ---------------------------------------------------------
    # ICEBREAKERS
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS icebreakers (
            id BIGSERIAL PRIMARY KEY,
            text TEXT NOT NULL UNIQUE,
            active INT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_icebreakers (
            user_id BIGINT NOT NULL REFERENCES users(user_id),
            icebreaker_id BIGINT NOT NULL REFERENCES icebreakers(id),
            used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, icebreaker_id)
        )
    """)

    # ---------------------------------------------------------
    # ADMIN AUDIT LOG
    # Texts of private thank-yous are NEVER copied here.
    # ---------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id BIGSERIAL PRIMARY KEY,
            admin_user_id BIGINT NOT NULL REFERENCES users(user_id),
            action TEXT NOT NULL,
            target_user_id BIGINT REFERENCES users(user_id),
            object_type TEXT,
            object_id BIGINT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_admin_audit_created
        ON admin_audit_log(created_at DESC)
    """)
