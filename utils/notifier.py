# utils/notifier.py
import asyncio
import httpx  # Assurez-vous que 'httpx' est dans votre requirements.txt
import logging

class Notifier:
    def __init__(self, token: str, chat_id: str ):
        # Le logger sera injecté plus tard, on prépare un logger temporaire
        self.logger = logging.getLogger("Notifier_pre-init")
        
        if not token or not chat_id or 'YOUR' in token:
            self.logger.warning("Token ou chat_id Telegram non configuré. Les notifications seront désactivées.")
            self.enabled = False
            return
            
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.enabled = True
        self.message_queue = asyncio.Queue( )
        # On ne peut pas créer de tâche ici, on le fera dans une méthode d'initialisation
        self.worker_task = None

    def set_logger(self, logger):
        """Injecte le logger principal après l'initialisation."""
        self.logger = logger

    async def start_worker(self):
        """Démarre la tâche de fond pour envoyer les messages."""
        if self.enabled and not self.worker_task:
            self.worker_task = asyncio.create_task(self._message_worker())
            self.logger.info("Worker de notifications Telegram démarré.")

    async def stop_worker(self):
        """Arrête proprement le worker de notifications."""
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                self.logger.info("Worker de notifications Telegram arrêté.")

    async def _message_worker(self):
        """Une tâche de fond qui envoie les messages de la file d'attente un par un."""
        while True:
            message = await self.message_queue.get()
            try:
                async with httpx.AsyncClient( ) as client:
                    data = {'chat_id': self.chat_id, 'text': message, 'parse_mode': 'Markdown'}
                    response = await client.post(self.base_url, data=data, timeout=10)
                    if response.status_code != 200:
                        self.logger.error(f"Échec de l'envoi du message Telegram. Statut: {response.status_code}, Réponse: {response.text}")
            except Exception as e:
                self.logger.error(f"Exception dans le worker de messages Telegram: {e}")
            finally:
                self.message_queue.task_done()
                await asyncio.sleep(1) # Limite à 1 message par seconde pour éviter le spam

    async def send_message(self, message: str):
        """Méthode publique pour mettre un message en file d'attente pour envoi."""
        if not self.enabled:
            return
        try:
            # Utilise await put() pour gérer la contre-pression si la file est pleine
            await self.message_queue.put(message)
        except Exception as e:
            self.logger.warning(f"Impossible de mettre le message en file d'attente Telegram: {e}")

