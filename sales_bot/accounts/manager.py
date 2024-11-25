import logging
from typing import Optional

from db.models import AccountStatus
from db.queries import AccountQueries, get_db

from .client import AccountClient
from .models import Account
from .safety import AccountSafety

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, db):
        self.db = db
        self.safety = AccountSafety()
        self._active_clients: dict[int, AccountClient] = {}

    async def add_account(self, phone: str) -> Optional[Account]:
        """Add new account to system"""
        try:
            async with get_db() as session:
                queries = AccountQueries(session)
                account = await queries.create_account(phone)
                if not account:
                    return None

            # Create and connect client
            client = AccountClient(account)
            if not await client.connect():
                return None

            # Request authorization code (will be sent to phone)
            await client.client.send_code_request(phone)

            return account

        except Exception as e:
            logger.error(f"Failed to add account {phone}: {e}", exc_info=True)
            return None

    async def authorize_account(self, phone: str, code: str) -> bool:
        """Authorize account with received code"""
        try:
            account = await self.queries.get_account_by_phone(phone)
            if not account:
                return False

            client = AccountClient(account)
            if not await client.connect():
                return False

            # Authorize and save session
            session_string = await client.authorize(code)
            if not session_string:
                return False

            # Update account in DB
            async with get_db() as session:
                queries = AccountQueries(session)
                return await queries.update_session(account.id, session_string)

        except Exception as e:
            logger.error(f"Failed to authorize account {phone}: {e}", exc_info=True)
            return False

    async def get_available_account(self) -> Optional[Account]:
        """Get account available for sending messages"""
        async with get_db() as session:
            queries = AccountQueries(session)
            accounts = await queries.get_active_accounts()

        for account in accounts:
            if account.is_available and self.safety.can_send_message(account):
                return account

        return None

    async def send_message(self, account: Account, username: str, text: str) -> bool:
        """Send message using specified account"""
        try:
            # Get or create client
            client = self._active_clients.get(account.id)
            if not client:
                client = AccountClient(account)
                if not await client.connect():
                    return False
                self._active_clients[account.id] = client

            # Check safety
            if not self.safety.can_send_message(account):
                return False

            # Send message
            success = await client.send_message(username, text)
            if success:
                # Update account stats
                async with get_db() as session:
                    queries = AccountQueries(session)
                    await queries.increment_messages(account.id)
                    self.safety.record_message(account)

            return success

        except Exception as e:
            logger.error(
                f"Failed to send message from {account.phone}: {e}", exc_info=True
            )
            return False

    async def update_account_status(self, phone: str, status: AccountStatus) -> bool:
        """Update account status"""
        try:
            async with get_db() as session:
                queries = AccountQueries(session)
                return await queries.update_account_status(phone, status)
        except Exception as e:
            logger.error(
                f"Failed to update account status for {phone}: {e}", exc_info=True
            )
            return False
