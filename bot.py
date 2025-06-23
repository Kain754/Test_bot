import logging
import os  # Для environment variables
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    CallbackContext,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    JobQueue
)
import psycopg2
from psycopg2 import pool  # Import connection pooling
import datetime
import asyncio  # Import asyncio

# Настройки
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")  # Or your database host
DATABASE_HOST = os.environ.get("DATABASE_HOST", "localhost")  # Default to localhost
DATABASE_USER = os.environ.get("DATABASE_USER")
DATABASE_PASSWORD = os.environ.get("DATABASE_PASSWORD")
DATABASE_NAME = os.environ.get("DATABASE_NAME")
ADMINISTRATOR_IDS = [int(admin_id) for admin_id in
                     os.environ.get("ADMINISTRATOR_IDS", "").split(",")]  # Example: "12345,67890"

# Состояния
(
    # Регистрация
    START, REGISTRATION_CONSENT, REGISTER_NAME, REGISTER_CONTACTS,
    REGISTER_TESERA, REGISTER_SOURCE, REGISTRATION_COMPLETE,
    # Админ-модерация
    ADMIN_MENU, REJECT_REASON,
    # Мероприятия
    EVENT_NAME, EVENT_TYPE, EVENT_DATE, EVENT_MAX_PARTICIPANTS,
    EVENT_DESCRIPTION, EVENT_CONFIRM,
    # Бронирование
    SHOW_EVENTS, SELECT_EVENT, CONFIRM_BOOKING, BOOKING_COMPLETE
) = range(19)

# Логирование
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Пул соединений PostgreSQL
connection_pool = None

# --- Инициализация базы данных ---
def init_db():
    global connection_pool
    connection_pool = psycopg2.pool.SimpleConnectionPool(
        1, 5,
        host=DATABASE_HOST,
        user=DATABASE_USER,
        password=DATABASE_PASSWORD,
        database=DATABASE_NAME
    )

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                last_name TEXT,
                contacts TEXT NOT NULL,
                tesera_nick TEXT,
                source TEXT,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                registration_date TIMESTAMP DEFAULT NOW()
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                type TEXT NOT NULL,
                date_start TIMESTAMP NOT NULL,
                date_end TIMESTAMP,
                location TEXT,
                max_participants INTEGER,
                current_participants INTEGER DEFAULT 0,
                price INTEGER,
                payment_required BOOLEAN DEFAULT FALSE,
                google_form_link TEXT,
                status TEXT DEFAULT 'active',
                created_by BIGINT REFERENCES users(user_id)
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS event_participants (
                participant_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                event_id INTEGER REFERENCES events(event_id),
                booking_status TEXT NOT NULL,
                payment_status TEXT,
                booking_date TIMESTAMP DEFAULT NOW(),
                UNIQUE (user_id, event_id)
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                event_id INTEGER REFERENCES events(event_id),
                amount INTEGER NOT NULL,
                currency TEXT DEFAULT 'RUB',
                method TEXT,
                receipt_photo TEXT,
                status TEXT DEFAULT 'pending',
                admin_comment TEXT,
                payment_date TIMESTAMP DEFAULT NOW()
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id SERIAL PRIMARY KEY,
                event_id INTEGER REFERENCES events(event_id),
                invite_link TEXT,
                rules TEXT,
                is_active BOOLEAN DEFAULT TRUE
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                event_id INTEGER REFERENCES events(event_id),
                message TEXT NOT NULL,
                type TEXT NOT NULL,
                is_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_actions (
                action_id SERIAL PRIMARY KEY,
                admin_id BIGINT REFERENCES users(user_id),
                target_user_id BIGINT REFERENCES users(user_id),
                action_type TEXT NOT NULL,
                details TEXT,
                action_time TIMESTAMP DEFAULT NOW()
            )
        """
        )

        conn.commit()
        print("Database initialized.")
    except psycopg2.Error as e:
        logger.error(f"DB init error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)


# --- Функции для работы с базой данных ---
def get_db_connection():
    return connection_pool.getconn()


def return_db_connection(conn):
    connection_pool.putconn(conn)  # Ускоряем работу запросов, очищая коннекты


# Команда /start
async def start(update: Update, context: CallbackContext) -> int:
    """Starts the conversation."""
    logger.info(f"User {update.effective_user.id} started the bot")

    keyboard = [
        [InlineKeyboardButton("Регистрация", callback_data="start_registration")]
    ]

    try:
        await update.message.reply_text(
            "Добро пожаловать в бота для мероприятий!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return START
    except Exception as e:
        logger.error(f"Error in start: {e}")
        return ConversationHandler.END


# Запрос согласия на обработку персональных данных
async def start_registration(update: Update, context: CallbackContext) -> int:
    """Asks the user for consent to process personal data."""
    query = update.callback_query
    await query.answer()

    consent_text = (
        "Для регистрации нам потребуется обработать ваши персональные данные. \n\n"
        "Имя и контакты - для связи.\n"
        "Продолжая, вы соглашаетесь с нашей политикой конфиденциальности."
    )

    keyboard = [
        [InlineKeyboardButton("Продолжить регистрацию", callback_data="continue_registration")],
        [InlineKeyboardButton("Отмена", callback_data="cancel")]
    ]

    await query.edit_message_text(
        consent_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return REGISTRATION_CONSENT


# Запрос имени
async def ask_name(update: Update, context: CallbackContext) -> int:
    """Asks the user to enter their name."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите ваше имя и фамилию:")
    return REGISTER_NAME


# Обработчик ввода имени
async def save_name(update: Update, context: CallbackContext) -> int:
    """Saves the user's name."""
    context.user_data["name"] = update.message.text
    await update.message.reply_text(
        "Введите контакты для связи (Telegram, VK или телефон): \n"
        "Пример: Telegram: @username / +79991112233"
    )
    return REGISTER_CONTACTS


# Обработчик контактов
async def save_contacts(update: Update, context: CallbackContext) -> int:
    """Saves the user's contact information."""
    context.user_data["contacts"] = update.message.text
    await update.message.reply_text(
        "Укажите ваш ник на Тесере (если есть, или нажмите /skip):"
    )
    return REGISTER_TESERA

# Обработчик ника в Тесера
async def save_tesera(update: Update, context: CallbackContext) -> int:
    """Saves the user's tesera nick."""
    context.user_data["tesera_nick"] = update.message.text
    await update.message.reply_text(
        "Откуда вы узнали о наших мероприятиях?\n"
        "Пример: «От друзей», «Из группы VK»"
    )
    return REGISTER_SOURCE


# Пропуск ника
async def skip_tesera(update: Update, context: CallbackContext) -> int:
    """Skips the Tesera nick entry."""
    context.user_data["tesera_nick"] = None
    await update.message.reply_text(
        "Откуда вы узнали о наших мероприятиях?\n"
        "Пример: «От друзей», «Из группы VK»"
    )
    return REGISTER_SOURCE


# Сохранение в БД
async def save_registration(update: Update, context: CallbackContext) -> int:
    """Saves registration data to the database."""
    user_data = context.user_data
    user = update.message.from_user

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (
                user_id, username, first_name, contacts, tesera_nick, source, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        """, (
            user.id,
            user.username,
            user_data["name"],
            user_data["contacts"],
            user_data.get("tesera_nick"),
            update.message.text
        ))
        conn.commit()
        await update.message.reply_text(
            "Регистрация завершена! Ожидайте подтверждения администратора."
        )
        await notify_admins(context, user.id, user_data["name"])
        context.user_data.clear()  # clear the user data after the conversation
    except psycopg2.Error as e:
        await update.message.reply_text("Ошибка при сохранении данных. Попробуйте позже.")
        logger.error(f"DB error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    return ConversationHandler.END


# Уведомление админа
async def notify_admins(context: CallbackContext, user_id: int, user_name: str) -> None:
    """Notifies administrators about a new registration."""
    message = (
        "⚠️ Новая заявка на регистрацию!\n"
        f"ID: {user_id}\n"
        f"Имя: {user_name}\n\n"
        "Подтвердить или отклонить?"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ]

    for admin_id in ADMINISTRATOR_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Failed to send message to admin {admin_id}: {e}")

# Команда /admin
async def admin_menu(update: Update, context: CallbackContext) -> int:
    """Displays the admin menu."""
    user_id = update.effective_user.id
    if user_id not in ADMINISTRATOR_IDS:
        await update.message.reply_text("🚫 Доступ запрещён.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("Список заявок", callback_data="list_pending")],
        [InlineKeyboardButton("Выйти", callback_data="cancel")]
    ]
    await update.message.reply_text(
        "Админ-панель. Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_MENU


# Просмотр заявок
async def list_pending_users(update: Update, context: CallbackContext) -> int:
    """Lists pending user registrations."""
    query = update.callback_query
    await query.answer()

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, first_name, contacts
            FROM users
            WHERE status = 'pending'
        """)
        pending_users = cursor.fetchall()

        if not pending_users:
            await query.edit_message_text("Нет заявок на регистрацию.")
            return ADMIN_MENU

        message = "📝 Список заявок:\n\n"
        for user_id, name, contacts in pending_users:
            message += f"ID: {user_id}\nИмя: {name}\nКонтакты: {contacts}\n\n"

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="back_to_admin")]
            ])
        )
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("Ошибка при загрузке данных.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    return ADMIN_MENU


# Подтверждение заявки
async def approve_user(update: Update, context: CallbackContext) -> None:
    """Обработчик подтверждения регистрации"""
    query = update.callback_query
    await query.answer()

    # Извлекаем user_id из callback_data (формат "approve_12345")
    user_id = int(query.data.split("_")[1])

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Обновляем статус в базе данных
        cursor.execute("""
            UPDATE users 
            SET status = 'approved' 
            WHERE user_id = %s
        """, (user_id,))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text="🎉 Ваша регистрация подтверждена! Теперь вы можете записываться на мероприятия."
        )

        # Редактируем сообщение с кнопками
        await query.edit_message_text(
            text=f"✅ Пользователь {user_id} подтверждён.",
            reply_markup=None  # Убираем кнопки после нажатия
        )

    except Exception as e:
        logger.error(f"Ошибка при подтверждении пользователя: {e}")
        await query.edit_message_text("❌ Ошибка при подтверждении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)


async def reject_user_callback(update: Update, context: CallbackContext) -> int:
    """Обработчик кнопки отклонения (первый шаг - запрос причины)"""
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.split("_")[1])
    context.user_data["reject_user_id"] = user_id

    # Редактируем сообщение для ввода причины
    await query.edit_message_text(
        text="Укажите причину отказа (или отправьте /cancel):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Энергетическая несовместимость", callback_data="default_reason")]
        ])
    )
    return REJECT_REASON


async def save_rejection_reason(update: Update, context: CallbackContext) -> int:
    """Обработчик сохранения причины отказа"""
    user_id = context.user_data["reject_user_id"]

    # Определяем причину (из кнопки или текстового сообщения)
    if update.callback_query and update.callback_query.data == "default_reason":
        reason = "Энергетическая несовместимость"
        await update.callback_query.answer()
    else:
        reason = update.message.text

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Обновляем статус в базе данных
        cursor.execute("""
            UPDATE users 
            SET status = 'rejected', rejection_reason = %s 
            WHERE user_id = %s
        """, (reason, user_id))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Ваша регистрация отклонена. Причина: {reason}"
        )

        # Отправляем подтверждение админу
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=f"❌ Пользователь {user_id} отклонён. Причина: {reason}",
                reply_markup=None
            )
        else:
            await update.message.reply_text(
                f"❌ Пользователь {user_id} отклонён. Причина: {reason}"
            )

    except Exception as e:
        logger.error(f"Ошибка при отклонении пользователя: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Ошибка при отклонении.")
        else:
            await update.message.reply_text("❌ Ошибка при отклонении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)
        context.user_data.clear()

    return ConversationHandler.END


# Запуск создания события
async def create_event(update: Update, context: CallbackContext) -> int:
    """Starts the event creation process."""
    # Проверка прав (только админы или все пользователи?)
    if update.effective_user.id not in ADMINISTRATOR_IDS:
        await update.message.reply_text("❌ Создавать мероприятия могут только администраторы.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Введите название мероприятия:\n"
        "Пример: «Аджилити Кемп 2024»"
    )
    return EVENT_NAME


# Название события
async def save_event_name(update: Update, context: CallbackContext) -> int:
    """Saves the event name."""
    context.user_data["event_name"] = update.message.text
    keyboard = [
        [InlineKeyboardButton("Кемп", callback_data="camp")],
        [InlineKeyboardButton("Игротека", callback_data="game_event")],
        [InlineKeyboardButton("Другое", callback_data="other")]
    ]
    await update.message.reply_text(
        "Выберите тип мероприятия:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EVENT_TYPE


# Тип мероприятия
async def save_event_type(update: Update, context: CallbackContext) -> int:
    """Saves the event type."""
    query = update.callback_query
    await query.answer()
    event_type = query.data
    context.user_data["event_type"] = event_type

    if event_type == "camp":
        await query.edit_message_text(
            "Введите дату и время кемпа (формат: ДД.ММ.ГГГГ ЧЧ:ММ):\n"
            "Пример: 25.12.2025 14:00"
        )
        return EVENT_DATE
    else:
        await query.edit_message_text(
            "Введите максимальное число участников (цифра):\n"
            "Пример: 10"
        )
        return EVENT_MAX_PARTICIPANTS


# Дата и время
async def save_event_date(update: Update, context: CallbackContext) -> int:
    """Saves the event date."""
    try:
        date_str = update.message.text
        date_obj = datetime.datetime.strptime(date_str, "%d.%m.%Y %H:%M")  # fixed bug here
        context.user_data["event_date"] = date_obj.isoformat()

        await update.message.reply_text(
            "Введите максимальное число участников: \n"
            "Пример: 15"
        )
        return EVENT_MAX_PARTICIPANTS
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
        return EVENT_DATE


# Участники и описание
async def save_max_participants(update: Update, context: CallbackContext) -> int:
    """Saves the maximum number of participants."""
    try:
        max_participants = int(update.message.text)
        if max_participants <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return EVENT_MAX_PARTICIPANTS
        context.user_data["max_participants"] = max_participants
        await update.message.reply_text(
            "Добавьте описание мероприятия (или нажмите /skip):\n"
            "Пример: «Трехдневный кемп с обучением аджилити»"
        )
        return EVENT_DESCRIPTION
    except ValueError:
        await update.message.reply_text("❌ Введите число (например, 20).")
        return EVENT_MAX_PARTICIPANTS


async def skip_description(update: Update, context: CallbackContext) -> int:
    """Skips the event description."""
    context.user_data["description"] = None
    # Вместо вызова confirm_event напрямую, имитируем ввод пустого описания
    context.user_data["description"] = "Нет описания"  # или просто None
    return await confirm_event(update, context)  # Передаём update явно


# Подтверждение и сохранение
async def confirm_event(update: Update, context: CallbackContext) -> int:
    """Confirms the event details before saving."""
    event_data = context.user_data

    # Добавьте проверку на наличие описания
    description = event_data.get("description", "не указано")

    message = (
        "<b>Новое мероприятие</b>\n\n"
        f"Название: {event_data['event_name']}\n"
        f"Тип: {event_data['event_type']}\n"
        f"Дата: {event_data['event_date']}\n"
        f"Макс. участников: {event_data['max_participants']}\n"
        f"Описание: {description}"
    )

    keyboard = [
        [InlineKeyboardButton("Сохранить", callback_data="save_event")],
        [InlineKeyboardButton("Отменить", callback_data="cancel")]
    ]

    # Отправляем сообщение с кнопками
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    return EVENT_CONFIRM


async def save_event_to_db(update: Update, context: CallbackContext) -> int:
    """Saves the event to the database."""
    query = update.callback_query
    await query.answer()  # added await here
    event_data = context.user_data

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO events (
                name, type, date_start, max_participants, description, created_by, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
        """, (
            event_data["event_name"], event_data["event_type"], event_data["event_date"],
            event_data["max_participants"],
            event_data.get("description"), update.effective_user.id)  # used .get()
                       )
        conn.commit()
        await query.edit_message_text("Мероприятие сохранено!")
        context.user_data.clear()  # clear user data
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("Ошибка при сохранении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)
    return ConversationHandler.END


# Просмотр мероприятий
async def show_events(update: Update, context: CallbackContext) -> int:
    """Shows a list of active events."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT event_id, name, date_start, max_participants, current_participants
            FROM events
            WHERE status = 'active'
        """)
        events = cursor.fetchall()

        if not events:
            await update.message.reply_text("🎭 Активных мероприятий нет.")
            return ConversationHandler.END

        keyboard = []
        for event in events:
            event_id, name, date, max_p, current_p = event
            free_slots = max_p - current_p
            btn_text = f"{name} ({date.strftime('%d.%m.%Y')}) | 🆓 {free_slots}/{max_p}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"select_{event_id}")])

        keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel")])

        await update.message.reply_text(
            "📅 Выберите мероприятие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SHOW_EVENTS

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await update.message.reply_text("❌ Ошибка при загрузке мероприятий.")
        return ConversationHandler.END
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)


# Выбор мероприятий
async def select_event(update: Update, context: CallbackContext) -> int:
    """Handles the selection of an event."""
    query = update.callback_query
    await query.answer()
    event_id = int(query.data.split("_")[1])
    context.user_data["selected_event_id"] = event_id

    # Проверяем, есть ли уже запись
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM event_participants
            WHERE user_id = %s AND event_id = %s
        """, (query.from_user.id, event_id))
        if cursor.fetchone():
            await query.edit_message_text("⚠️ Вы уже записаны на это мероприятие.")
            return ConversationHandler.END

        # Проверяем свободные места
        cursor.execute("""
            SELECT max_participants, current_participants
            FROM events
            WHERE event_id = %s
        """, (event_id,))
        max_p, current_p = cursor.fetchone()
        if current_p >= max_p:
            await query.edit_message_text("❌ Мест больше нет.")
            return ConversationHandler.END

        # Запрашиваем подтверждение
        await query.edit_message_text(
            f"Подтвердите запись на мероприятие:\n\n"
            f"Свободных мест: {max_p - current_p}/{max_p}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_booking")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
            ])
        )
        return CONFIRM_BOOKING

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при бронировании.")
        return ConversationHandler.END
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)


# Подтверждение бронирования
async def confirm_booking(update: Update, context: CallbackContext) -> int:
    """Confirms the booking for an event."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    event_id = context.user_data["selected_event_id"]

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Записываем участника
        cursor.execute("""
            INSERT INTO event_participants
            (user_id, event_id, booking_status)
            VALUES (%s, %s, 'pending')
        """, (user_id, event_id))

        # Обновляем счётчик участников
        cursor.execute("""
            UPDATE events
            SET current_participants = current_participants + 1
            WHERE event_id = %s
        """, (event_id,))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text="🎟️ Ваша заявка принята! Ожидайте подтверждения."
        )

        # Уведомляем админа (если требуется модерация)
        await notify_admin_about_booking(context, user_id, event_id)

        await query.edit_message_text("✅ Запись завершена!")
        context.user_data.clear()  # clear user data
        return BOOKING_COMPLETE

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при сохранении.")
        return ConversationHandler.END
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

# Уведомление админа
async def notify_admin_about_booking(context: CallbackContext, user_id: int, event_id: int) -> None:
    """Notifies the admin about a new booking."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Получаем данные о мероприятии
        cursor.execute("""
            SELECT name FROM events WHERE event_id = %s
        """, (event_id,))
        event_name = cursor.fetchone()[0]

        # Получаем данные о пользователе
        cursor.execute("""
                    SELECT first_name, contacts FROM users WHERE user_id = %s
                """, (user_id,))
        user_name, contacts = cursor.fetchone()

        message = (
            "⚠️ Новая заявка на мероприятие!\n\n"
            f"Мероприятие: {event_name}\n"
            f"Участник: {user_name} (ID: {user_id})\n"
            f"Контакты: {contacts}\n\n"
            "Подтвердить запись?"
        )

        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_booking_{event_id}_{user_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_booking_{event_id}_{user_id}")]
        ]

        for admin_id in ADMINISTRATOR_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                logger.error(f"Failed to send message to admin {admin_id}: {e}")

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

# Подтверждение брони админом
async def approve_booking(update: Update, context: CallbackContext) -> None:
    """Approves the booking by the admin."""
    query = update.callback_query
    await query.answer()
    _, event_id, user_id = query.data.split("_")  # "approve_booking_123_456"

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                    UPDATE event_participants
                    SET booking_status = 'confirmed'
                    WHERE event_id = %s AND user_id = %s
                """, (event_id, user_id))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Ваша заявка на мероприятие одобрена!"
        )

        await query.edit_message_text(f"Заявка пользователя {user_id} подтверждена.")
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при подтверждении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Отклонение брони админом


async def reject_booking(update: Update, context: CallbackContext) -> None:
    """Rejects the booking by the admin."""
    query = update.callback_query
    await query.answer()
    _, event_id, user_id = query.data.split("_")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Уменьшаем счётчик участников
        cursor.execute("""
                            UPDATE events
                            SET current_participants = current_participants - 1
                            WHERE event_id = %s
                        """, (event_id,))

        # Удаляем запись
        cursor.execute("""
                            DELETE FROM event_participants
                            WHERE event_id = %s AND user_id = %s
                        """, (event_id, user_id))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Ваша заявка на мероприятие отклонена."
        )

        await query.edit_message_text(f"Заявка пользователя {user_id} отклонена.")
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при отклонении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Дорабатываем бронирование


async def confirm_booking(update: Update, context: CallbackContext) -> int:
    """Confirms the booking (with payment logic)."""
    query = update.callback_query
    await query.answer()
    event_id = context.user_data["selected_event_id"]
    user_id = query.from_user.id

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                            SELECT payment_required, price, type
                            FROM events
                            WHERE event_id = %s
                        """, (event_id,))
        payment_required, price, event_type = cursor.fetchone()

        if payment_required:
            # Сохраняем временную запись о бронировании
            cursor.execute("""
                                INSERT INTO event_participants
                                (user_id, event_id, booking_status, payment_status)
                                VALUES (%s, %s, 'pending', 'unpaid')
                            """, (user_id, event_id))

            # Отправляем реквизиты
            await send_payment_details(context, user_id, event_id, price)
            await query.edit_message_text("💳 Оплатите участие, чтобы завершить бронирование.")
        else:
            # Бесплатное мероприятие — сразу подтверждаем
            cursor.execute("""
                                INSERT INTO event_participants
                                (user_id, event_id, booking_status, payment_status)
                                VALUES (%s, %s, 'confirmed', 'not_required')
                            """, (user_id, event_id))
            await query.edit_message_text("✅ Запись завершена!")
            await notify_admin_about_booking(context, user_id, event_id)

        conn.commit()
        return BOOKING_COMPLETE

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при бронировании.")
        return ConversationHandler.END
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Отправка реквизитов


async def send_payment_details(context: CallbackContext, user_id: int, event_id: int, price: int) -> None:
    """Sends payment details to the user."""
    payment_message = (
        f"🔹 <b>Оплата мероприятия</b>\n\n"
        f"Сумма: {price} ₽\n"
        "Способ оплаты: <b>СБП</b>\n\n"
        "➔ Реквизиты для перевода:\n"
        "Банк: Тинькофф\n"
        "Номер: <code>+7 (XXX) XXX-XX-XX</code>\n\n"
        "Или переведите по ссылке: [Оплатить через СБП](https://qr.nspk.ru/...)\n\n"
        "После оплаты нажмите кнопку ниже ⤵️"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_payment_{event_id}")]
    ]

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=payment_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to send payment details to user {user_id}: {e}")

    # Подтверждение оплаты


async def handle_payment_confirmation(update: Update, context: CallbackContext) -> None:
    """Handles the payment confirmation from the user."""
    query = update.callback_query
    await query.answer()
    event_id = int(query.data.split("_")[2])
    user_id = query.from_user.id

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Помечаем оплату как "ожидает проверки"
        cursor.execute("""
                            UPDATE event_participants
                            SET payment_status = 'pending_verification'
                            WHERE user_id = %s AND event_id = %s
                        """, (user_id, event_id))
        conn.commit()

        # Уведомляем админа
        await notify_admin_about_payment(context, user_id, event_id)
        await query.edit_message_text("🔄 Платеж отправлен на проверку. Мы уведомим вас о подтверждении.")

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при обработке платежа.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Уведомление админа о платеже


async def notify_admin_about_payment(context: CallbackContext, user_id: int, event_id: int) -> None:
    """Notifies the admin about a new payment."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                            SELECT name, price FROM events WHERE event_id = %s
                        """, (event_id,))
        event_name, price = cursor.fetchone()

        cursor.execute("""
                            SELECT first_name FROM users WHERE user_id = %s
                        """, (user_id,))
        user_name = cursor.fetchone()[0]

        message = (
            "⚠️ Новый платеж для проверки!\n\n"
            f"Мероприятие: {event_name}\n"
            f"Участник: {user_name} (ID: {user_id})\n"
            f"Сумма: {price} ₽\n\n"
            "Подтвердить получение средств?"
        )

        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"verify_payment_{event_id}_{user_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_payment_{event_id}_{user_id}")]
        ]

        for admin_id in ADMINISTRATOR_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                logger.error(f"Failed to send message to admin {admin_id}: {e}")

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Подтверждение платежа


async def verify_payment(update: Update, context: CallbackContext) -> None:
    """Verifies the payment by the admin."""
    query = update.callback_query
    await query.answer()
    _, event_id, user_id = query.data.split("_")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Обновляем статусы
        cursor.execute("""
                            UPDATE event_participants
                            SET
                                payment_status = 'paid',
                                booking_status = 'confirmed'
                            WHERE user_id = %s AND event_id = %s
                        """, (user_id, event_id))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text="✅ Ваш платеж подтвержден! Бронирование активно."
        )

        await query.edit_message_text(f"Платеж пользователя {user_id} подтвержден.")

        # После подтверждения платежа:
        # Get event details
        cursor.execute("SELECT name FROM events WHERE event_id = %s", (event_id,))
        event_name = cursor.fetchone()[0]
        ADMIN_GROUP_ID = os.environ.get("ADMIN_GROUP_ID")

        if ADMIN_GROUP_ID:  # Only if the environment variable is set
            chat_title = f"Чат мероприятия: {event_name}"
            chat = await context.bot.create_chat_invite_link(
                chat_id=ADMIN_GROUP_ID,  # ID группы-шаблона
                name=chat_title
            )
            rules = os.environ.get("CHAT_RULES", "Правила не установлены")
            cursor.execute("""
                            INSERT INTO chats (event_id, invite_link, rules)
                            VALUES (%s, %s, %s)
                        """, (event_id, chat.invite_link, rules))
            conn.commit()

            # Invite to chat after payment
            await invite_to_chat(context, user_id, event_id)
        else:
            logger.warning("ADMIN_GROUP_ID not set. Skipping chat creation")

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при подтверждении.")
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        await query.edit_message_text("❌ Ошибка Telegram.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Отклонение платежа


async def reject_payment(update: Update, context: CallbackContext) -> None:
    """Rejects the payment by the admin."""
    query = update.callback_query
    await query.answer()
    _, event_id, user_id = query.data.split("_")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Возвращаем статус "не оплачено"
        cursor.execute("""
                            UPDATE event_participants
                            SET payment_status = 'rejected'
                            WHERE user_id = %s AND event_id = %s
                        """, (user_id, event_id))
        conn.commit()

        # Уведомляем пользователя
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Платеж не подтвержден. Пожалуйста, свяжитесь с администратором."
        )

        await query.edit_message_text(f"Платеж пользователя {user_id} отклонен.")
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("❌ Ошибка при отклонении.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Приглашение в чат


async def invite_to_chat(context: CallbackContext, user_id: int, event_id: int) -> None:
    """Invites a user to the event chat."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                            SELECT invite_link FROM chats WHERE event_id = %s
                        """, (event_id,))
        invite_link = cursor.fetchone()[0]

        await context.bot.send_message(
            chat_id=user_id,
            text=f"🔹 Вы подтверждены на мероприятие!\n"
                 f"Присоединяйтесь к чату: {invite_link}\n\n"
                 "Правила чата: ..."
        )
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Проверка предстоящих событий


async def check_upcoming_events(context: CallbackContext) -> None:
    """Checks for upcoming events and sends reminders."""
    conn = None
    cursor = None  # Initialize cursor here

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.datetime.now()
        cursor.execute("""
                        SELECT e.event_id, e.name, e.date_start,
                               c.invite_link, ep.user_id
                        FROM events e
                        JOIN event_participants ep ON e.event_id = ep.event_id
                        LEFT JOIN chats c ON e.event_id = c.event_id
                        WHERE ep.booking_status = 'confirmed'
                          AND e.date_start BETWEEN %s AND %s + INTERVAL '1 hour'
                    """, (now, now))
        events = cursor.fetchall()

        for event in events:
            event_id, name, start_time, invite_link, user_id = event
            time_left = start_time - now

            if time_left.total_seconds() <= 3600:  # 1 час
                await send_reminder(context, user_id, name, start_time, invite_link)

    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")

    except Exception as e:
        logger.error(f"Error in check_upcoming_events: {e}")

    finally:
        if cursor:  # Check if cursor is not None before closing
            cursor.close()
        if conn:
            return_db_connection(conn)

    # Отправка напоминания


async def send_reminder(context: CallbackContext, user_id: int, event_name: str,
                        start_time: datetime.datetime, invite_link: str) -> None:
    """Sends a reminder to the user about an upcoming event."""
    message = (
        f"⏰ Напоминание: мероприятие «{event_name}»\n"
        f"Начнётся через 1 час ({start_time.strftime('%H:%M')})\n\n"
        f"Чат: {invite_link}"
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=message)
    except Exception as e:
        logger.error(f"Failed to send reminder to user {user_id}: {e}")

    # Правила чата


async def send_rules(update: Update, context: CallbackContext) -> None:
    """Sends the chat rules to the user."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                        SELECT rules FROM chats
                        WHERE event_id = (SELECT event_id FROM event_participants
                                         WHERE user_id = %s LIMIT 1)
                    """, (update.effective_user.id,))
        rules = cursor.fetchone()

        await update.message.reply_text(rules[0] if rules else "Правила не установлены.")
    except psycopg2.Error as e:
        logger.error(f"DB error: {e}")
        await update.message.reply_text("Произошла ошибка при получении правил чата")
    except Exception as e:
        logger.error(f"Ошибка при отправке правил: {e}")
        await update.message.reply_text("Не удалось отправить правила чата.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_db_connection(conn)

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "Bye! I hope we can talk again some day.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def cancel_event_creation(update: Update, context: CallbackContext) -> int:
    """Отменяет создание мероприятия и очищает временные данные."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Создание мероприятия отменено.")
    else:
        await update.message.reply_text("❌ Создание мероприятия отменено.")

    # Очищаем временные данные
    context.user_data.clear()
    return ConversationHandler.END

async def main():
    """Main function to run the bot."""
    init_db()
    application = Application.builder().token(TOKEN).build()

    # Обработчик команды /start
    application.add_handler(CommandHandler("start", start))

    # Обработчики для регистрации
    registration_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_registration, pattern="^start_registration$")],
        states={
            REGISTRATION_CONSENT: [CallbackQueryHandler(ask_name, pattern="^continue_registration$")],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
            REGISTER_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_contacts)],
            REGISTER_TESERA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_tesera),
                CommandHandler("skip", skip_tesera)
            ],
            REGISTER_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_registration)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(registration_handler)

    # Обработчик создания мероприятий
    event_creation_handler = ConversationHandler(
        entry_points=[CommandHandler('create_event', create_event)],
        states={
            EVENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_event_name)],
            EVENT_TYPE: [CallbackQueryHandler(save_event_type)],
            EVENT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_event_date)],
            EVENT_MAX_PARTICIPANTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_max_participants)],
            EVENT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_event),
                CommandHandler('skip', skip_description)
            ],
            EVENT_CONFIRM: [
                CallbackQueryHandler(save_event_to_db, pattern="^save_event$"),
                CallbackQueryHandler(cancel_event_creation, pattern="^cancel$")
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_event_creation)]
    )
    application.add_handler(event_creation_handler)

    # Обработчики админ-панели
    application.add_handler(CommandHandler("admin", admin_menu))
    admin_handlers = [
        CallbackQueryHandler(list_pending_users, pattern="^list_pending$"),
        CallbackQueryHandler(admin_menu, pattern="^back_to_admin$"),
        CallbackQueryHandler(approve_user, pattern="^approve_"),
        CallbackQueryHandler(reject_user_callback, pattern="^reject_"),
        CallbackQueryHandler(save_rejection_reason, pattern="^default_reason$")
    ]
    application.add_handlers(admin_handlers)

    # Планировщик напоминаний
    if application.job_queue:
        application.job_queue.run_repeating(check_upcoming_events, interval=1800)

    # Запуск бота
    async with application:
        await application.start()
        await application.updater.start_polling()
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    # Создаем новый event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    finally:
        loop.close()
        if connection_pool:
            connection_pool.closeall()
