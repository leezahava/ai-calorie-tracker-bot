import os
import asyncio
import logging
import sqlite3
import json
import re
import time
import io
from datetime import datetime, timedelta
from typing import Optional, Any, Dict
from contextlib import contextmanager

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
    FSInputFile
)
from dotenv import load_dotenv
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError
from google import genai
from google.genai import types as genai_types

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
REG_PASSWORD: str = os.getenv("REGISTRATION_PASSWORD", "Hava2026")

if not BOT_TOKEN:
    raise ValueError("Error: TELEGRAM_BOT_TOKEN not found in .env file!")
if not GEMINI_API_KEY:
    raise ValueError("Error: GEMINI_API_KEY not found in .env file!")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "users_data.db")
HAMSTER_IMAGE_PATH = os.path.join(BASE_DIR, "hamster.png")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)
scheduler = AsyncIOScheduler()
LAST_REQUESTS: Dict[int, float] = {}

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db() -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, gender TEXT, weight REAL, height REAL, age INTEGER, activity_level TEXT, palm_size REAL, bmi REAL, goal TEXT, daily_norm INTEGER)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS food_logs (log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date_time TEXT, dish_name TEXT, calories REAL, protein REAL, fat REAL, carbs REAL, water_ml INTEGER DEFAULT 0, is_burn INTEGER DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS reminders (reminder_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, remind_time TEXT, remind_text TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS weight_history (log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date_str TEXT, weight_real REAL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS allowed_users (user_id INTEGER PRIMARY KEY, auth_date TEXT)''')

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_ai_response(prompt: str, image_bytes: Optional[bytes] = None) -> Any:
    config = genai_types.GenerateContentConfig(response_mime_type="application/json")
    if image_bytes:
        return ai_client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=[genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'), prompt], 
            config=config
        )
    return ai_client.models.generate_content(
        model='gemini-2.5-flash', 
        contents=prompt, 
        config=config
    )

def is_throttled(user_id: int, limit: int = 2) -> bool:
    current_time = time.time()
    if current_time - LAST_REQUESTS.get(user_id, 0.0) < limit:
        return True
    LAST_REQUESTS[user_id] = current_time
    return False

def calculate_daily_calories(gender: str, weight: float, height: float, age: int, activity_str: str, goal_str: str = "🔄 Maintenance") -> int:
    if gender == "Male":
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    elif gender == "Female":
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 78
        
    coeff = 1.2 if activity_str.startswith("1)") else 1.375 if activity_str.startswith("2)") else 1.55 if activity_str.startswith("3)") else 1.725
    maintenance = bmr * coeff 
    
    if goal_str == "📉 Weight Loss":
        return round(maintenance * 0.85)
    elif goal_str == "📈 Weight Gain":
        return round(maintenance * 1.15)
    return round(maintenance)

def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Nutrition Stats"), KeyboardButton(text="📈 Weight Progress")],
        [KeyboardButton(text="💧 Log Water"), KeyboardButton(text="🔥 Log Activity")],
        [KeyboardButton(text="⚖️ Update Weight"), KeyboardButton(text="⚙️ Edit Profile")],
        [KeyboardButton(text="🔔 My Reminders"), KeyboardButton(text="🚨 Reset All")]
    ], resize_keyboard=True)

class SecurityStates(StatesGroup):
    waiting_for_password = State()

class RegistrationStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_gender = State()
    waiting_for_weight = State()
    waiting_for_height = State()
    waiting_for_age = State()
    waiting_for_activity = State()
    waiting_for_palm = State()
    waiting_for_goal = State()
    waiting_for_custom_calories = State()

class CorrectionStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_product_weight = State()

class EditStates(StatesGroup):
    waiting_for_new_name = State()
    waiting_for_new_age = State()
    waiting_for_new_weight = State()

class ReminderStates(StatesGroup):
    waiting_for_time = State()
    waiting_for_text = State()

class ActivityStates(StatesGroup):
    waiting_for_activity_text = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    if not message.from_user:
        return
    await state.clear()
    user_id = message.from_user.id
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM allowed_users WHERE user_id = ?", (user_id,))
        is_allowed = cursor.fetchone()
        
    if not is_allowed:
        await state.set_state(SecurityStates.waiting_for_password)
        await message.answer(
            "🔒 **Access Restricted (Security System)**\n\n"
            "This AI tracker is currently running in a closed beta testing mode.\n"
            "Please enter the secret access password to unlock all features:",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    start_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Create Profile & Get Started", callback_data="start_registration")]
    ])
    
    onboarding_text = (
        "👋 Welcome!\n\n"
        "I am your intelligent nutrition assistant powered by Gemini AI.\n"
        "No more manual macro logging—let artificial intelligence handle it for you!\n\n"
        "🌟 **Key Features:**\n"
        "📸 **Photo Analysis:** Take a picture of your meal next to your palm, and I'll calculate macros.\n"
        "🏪 **Barcode Scanning:** Send a photo of a product package or barcode.\n"
        "💧 **Water Tracker** & 🏃 **Activity Calculator**.\n"
        "📊 **Graphical Analytics** of your weight progress.\n\n"
        "Click the button below to set up your personal profile 👇"
    )
    
    if os.path.exists(HAMSTER_IMAGE_PATH):
        try:
            await message.answer_photo(photo=FSInputFile(HAMSTER_IMAGE_PATH), caption=onboarding_text, reply_markup=start_kb)
        except Exception:
            await message.answer(onboarding_text, reply_markup=start_kb)
    else:
        await message.answer(onboarding_text, reply_markup=start_kb)

@dp.message(SecurityStates.waiting_for_password)
async def process_security_password(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
        
    if message.text.strip() == REG_PASSWORD:
        today_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            conn.cursor().execute("INSERT OR REPLACE INTO allowed_users (user_id, auth_date) VALUES (?, ?)", (message.from_user.id, today_str))
        await message.answer("🎉 Authorization successful! Access granted.")
        await cmd_start(message, state)
    else:
        await message.answer("❌ Invalid access code! Please try again:")

@dp.callback_query(F.data == "start_registration")
async def cb_start_registration(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not isinstance(callback.message, types.Message):
        return
    await callback.message.delete() 
    await callback.message.answer("✍️ Please enter your name or how I should address you:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_name)
    await callback.answer()

@dp.message(RegistrationStates.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Male"), KeyboardButton(text="Female"), KeyboardButton(text="Prefer not to say")]
    ], resize_keyboard=True, one_time_keyboard=True)
    await message.answer(f"Nice to meet you, {message.text}! Now please specify your gender for accurate basal metabolic rate calculation:", reply_markup=kb)
    await state.set_state(RegistrationStates.waiting_for_gender)

@dp.message(RegistrationStates.waiting_for_gender)
async def process_gender(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(gender=message.text)
    await message.answer("⚖️ Enter your current weight in kilograms (e.g., 65.5):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_weight)

@dp.message(RegistrationStates.waiting_for_weight)
async def process_weight(message: types.Message, state: FSMContext):
    if not message.text:
        return
    try:
        weight = float(message.text.replace(",", "."))
        await state.update_data(weight=weight)
        await message.answer("📏 Enter your height in centimeters (e.g., 170):")
        await state.set_state(RegistrationStates.waiting_for_height)
    except ValueError:
        await message.answer("⚠️ Please enter a valid numerical weight value.")

@dp.message(RegistrationStates.waiting_for_height)
async def process_height(message: types.Message, state: FSMContext):
    if not message.text:
        return
    try:
        height = float(message.text.replace(",", "."))
        await state.update_data(height=height)
        await message.answer("🎂 Enter your age (full years):")
        await state.set_state(RegistrationStates.waiting_for_age)
    except ValueError:
        await message.answer("⚠️ Please enter a valid numerical height value.")

@dp.message(RegistrationStates.waiting_for_age)
async def process_age(message: types.Message, state: FSMContext):
    if not message.text:
        return
    try:
        age = int(message.text)
        await state.update_data(age=age)
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="1) Sedentary (desk job, low steps)")],
                [KeyboardButton(text="2) Moderate (5k-10k steps, light workouts)")],
                [KeyboardButton(text="3) Active (10k+ steps, regular workouts)")],
                [KeyboardButton(text="4) Highly Active (heavy labor, daily sports)")]
            ], resize_keyboard=True, one_time_keyboard=True
        )
        await message.answer("🏃 Select your physical activity level:", reply_markup=kb)
        await state.set_state(RegistrationStates.waiting_for_activity)
    except ValueError:
        await message.answer("⚠️ Please enter your age as a whole number.")

@dp.message(RegistrationStates.waiting_for_activity)
async def process_activity(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(activity_level=message.text)
    await message.answer("🖐 Enter your hand/palm size in cm (from wrist to tip of middle finger, e.g., 16.5).\nThis helps the AI accurately estimate portion sizes from photos:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_palm)

@dp.message(RegistrationStates.waiting_for_palm)
async def process_palm(message: types.Message, state: FSMContext):
    if not message.text:
        return
    try:
        palm = float(message.text.replace(",", "."))
        await state.update_data(palm_size=palm)
        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📉 Weight Loss")], 
            [KeyboardButton(text="🔄 Maintenance")], 
            [KeyboardButton(text="📈 Weight Gain")]
        ], resize_keyboard=True, one_time_keyboard=True)
        await message.answer("🎯 What is your primary fitness goal?", reply_markup=kb)
        await state.set_state(RegistrationStates.waiting_for_goal)
    except ValueError:
        await message.answer("⚠️ Please enter your palm size as a valid number.")

@dp.message(RegistrationStates.waiting_for_goal)
async def process_goal(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(goal=message.text)
    ud = await state.get_data()
    dn = calculate_daily_calories(ud.get('gender',''), ud.get('weight',0.0), ud.get('height',0.0), ud.get('age',0), ud.get('activity_level',''), message.text)
    await state.update_data(calculated_calories=dn)
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✅ Accept this value")], 
        [KeyboardButton(text="✏️ Enter custom calorie goal")]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await message.answer(f"📊 Based on your data, the scientific formula calculated your optimal intake:\n🔥 **{dn} kcal/day**.\n\nWould you like to accept this value or manually set your own daily limit?", reply_markup=kb)
    await state.set_state(RegistrationStates.waiting_for_custom_calories)

@dp.message(RegistrationStates.waiting_for_custom_calories)
async def process_custom_calories(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
        
    ud = await state.get_data()
    user_id = message.from_user.id
    
    if message.text == "✅ Accept this value":
        fc = ud.get('calculated_calories', 2000)
    else:
        try:
            fc = int(message.text)
            if fc < 800:
                await message.answer("⚠️ The daily limit cannot be less than 800 kcal. Please enter a valid target:")
                return
        except ValueError:
            await message.answer("⚠️ Please enter the calorie target as a whole number:")
            return

    today_str = datetime.now().strftime("%Y-%m-%d")
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?)', 
            (user_id, ud.get('name'), ud.get('gender'), ud.get('weight'), ud.get('height'), ud.get('age'), ud.get('activity_level'), ud.get('palm_size'), round(ud.get('weight',0.0)/((ud.get('height',0.0)/100)**2),1), ud.get('goal'), fc)
        )
        cursor.execute('INSERT OR REPLACE INTO weight_history (user_id, date_str, weight_real) VALUES (?, ?, ?)', (user_id, today_str, ud.get('weight')))
    
    await message.answer(f"🎉 Profile successfully created, {ud.get('name')}!\n\n🎯 Goal: {ud.get('goal')}\n🔥 Daily Limit: {fc} kcal/day\n\nNow you can start logging meals by simply sending photos!", reply_markup=get_main_menu())
    await state.clear()

@dp.message(F.text == "⚙️ Edit Profile")
async def cmd_edit_profile(message: types.Message):
    await message.answer(
        "🛠 **Your Personal Profile Settings Panel**\n\n"
        "Which information would you like to update to help me keep your metrics accurate?", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Change Name", callback_data="edit_name"), InlineKeyboardButton(text="🎂 Age", callback_data="edit_age")],
            [InlineKeyboardButton(text="🔄 Reset & Re-enter ALL data", callback_data="start_registration")]
        ])
    )

@dp.message(F.text == "⚖️ Update Weight")
async def cmd_update_weight_direct(message: types.Message, state: FSMContext):
    await message.answer("⚖️ Enter your latest weight in kilograms (e.g., 65.5):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(EditStates.waiting_for_new_weight)

@dp.callback_query(F.data.startswith("edit_"))
async def cb_edit_field(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not isinstance(callback.message, types.Message) or not callback.data:
        return
        
    field = callback.data.split("_")[1]
    if field == "name":
        await callback.message.answer("✍️ Please enter your new name:")
        await state.set_state(EditStates.waiting_for_new_name)
    elif field == "age":
        await callback.message.answer("🎂 Enter your updated age:")
        await state.set_state(EditStates.waiting_for_new_age)
    await callback.answer()

@dp.message(EditStates.waiting_for_new_name)
async def proc_new_name(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    with get_db() as conn:
        conn.cursor().execute("UPDATE users SET name = ? WHERE user_id = ?", (message.text, message.from_user.id))
    await message.answer(f"✅ Success! I will now address you as **{message.text}**.", reply_markup=get_main_menu())
    await state.clear()

@dp.message(EditStates.waiting_for_new_age)
async def proc_new_age(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    try:
        new_age = int(message.text)
        with get_db() as conn:
            conn.cursor().execute("UPDATE users SET age = ? WHERE user_id = ?", (new_age, message.from_user.id))
        await message.answer(f"✅ Age updated to {new_age} years.", reply_markup=get_main_menu())
        await state.clear()
    except ValueError:
        await message.answer("⚠️ Please enter age as a whole number.")

@dp.message(EditStates.waiting_for_new_weight)
async def proc_new_weight(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    try:
        nw = float(message.text.replace(",", "."))
        user_id = message.from_user.id
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT weight, goal, gender, height, age, activity_level FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                nn = calculate_daily_calories(row[2], nw, row[3], row[4], row[5], row[1])
                cursor.execute("UPDATE users SET weight = ?, bmi = ?, daily_norm = ? WHERE user_id = ?", (nw, round(nw/((row[3]/100)**2),1), nn, user_id))
                cursor.execute('INSERT OR REPLACE INTO weight_history (user_id, date_str, weight_real) VALUES (?, ?, ?)', (user_id, today_str, nw))
                
        await message.answer(f"✅ Weight updated! Data point added to your progress history.\nYour new daily target is: {nn} kcal.", reply_markup=get_main_menu())
        await state.clear()
    except ValueError:
        await message.answer("⚠️ Please enter a valid number for weight.")

@dp.message(F.photo)
async def handle_food_photo_date_request(message: types.Message, state: FSMContext):
    if not message.photo or not message.from_user or is_throttled(message.from_user.id, limit=3):
        return
    
    await state.update_data(
        temp_photo_file_id=message.photo[-1].file_id,
        photo_caption=message.caption or ""
    )
    
    t = datetime.now().strftime("%Y-%m-%d")
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Yesterday", callback_data=f"setdate_{y}"), InlineKeyboardButton(text="🍏 Today", callback_data=f"setdate_{t}")]
    ])
    await message.answer("📅 Which day should this meal or product log be assigned to?", reply_markup=kb)

@dp.callback_query(F.data.startswith("setdate_"))
async def process_photo_with_date(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not callback.from_user or not callback.data or not isinstance(callback.message, types.Message):
        return
    
    await callback.answer() 
    chosen_date = callback.data.split("_")[1]
    fsm_data = await state.get_data()
    await state.update_data(target_date=chosen_date)
    
    photo_id = fsm_data.get("temp_photo_file_id")
    user_caption = fsm_data.get("photo_caption", "")
    
    await callback.message.edit_reply_markup(reply_markup=None)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT palm_size FROM users WHERE user_id = ?", (callback.from_user.id,))
        user_row = cursor.fetchone()
    
    if not user_row or not photo_id or not isinstance(photo_id, str):
        return
    
    status_msg = await callback.message.answer("📸 Image received!\n🧠 Sending to Gemini AI for analysis... This typically takes up to 10 seconds.")

    try:
        file_info = await bot.get_file(photo_id)
        photo_bytes = await bot.download_file(file_info.file_path)
        if photo_bytes is None:
            return
        image_data = photo_bytes.read()

        caption_instruction = f"\nThe user added text with the photo: '{user_caption}'. CRITICAL: Utilize this text context to identify the food items and drinks accurately!" if user_caption else ""

        prompt = f"""
        You are a highly qualified expert AI dietitian. Your job is to analyze the image and return the output EXCLUSIVELY in JSON format. Never return 0 calories for actual food. Provide all descriptions and text inside the JSON in English language only. {caption_instruction}
        
        IMPORTANT: If there are multiple different food items or drinks in the image (e.g., cottage cheese with jam and tea), COMBINE them into a single response object. Sum all calories and macronutrients together, and combine their names inside 'dish_name' separated by a plus sign (e.g., 'Cottage Cheese with Jam + Berry Juice'). ALWAYS return EXACTLY ONE flat object {{...}}. Never return an array or list.

        Classification Rules:
        1. CATEGORY 'barcode':
           Format: {{"type": "barcode", "barcode_digits": "digits_without_spaces", "dish_name": "Product_Name"}}

        2. CATEGORY 'food' (Commercial factory packaging, standardized manufactured snacks):
           STRICT SCALE RULE: If the photo contains exactly ONE standard piece of chocolate candy, its real weight is STRICTLY 10-14 grams. DO NOT overestimate weight.
           Format: {{"type": "food", "status": "success", "dish_name": "Precise name (approximate weight in grams)", "calories": number, "protein": number, "fat": number, "carbs": number, "scale_method": "standard_weight"}}

        3. CATEGORY 'open_dish' (Cooked meal on a plate, raw food or poured drink):
           - If a human hand/palm is visible: Use its known size ({user_row[0]} cm) as a reference scale. Set "scale_method": "palm".
           - If no hand is visible: Form an expert visual estimation of the volume. Set "scale_method": "visual".
           Format: {{"type": "open_dish", "status": "success", "dish_name": "Dish Name", "calories": number, "protein": number, "fat": number, "carbs": number, "scale_method": "visual"}}
        """
        
        response = get_ai_response(prompt, image_data)
        
        try:
            response_text = response.text
        except ValueError as safety_error:
            logging.error(f"Safety Block Error: {safety_error}")
            await status_msg.edit_text("⚠️ Google AI filtered this image due to safety protocols. Please take a photo from a different angle.")
            return

        if not response_text:
            await status_msg.edit_text("⚠️ AI returned an empty response.")
            return
            
        logging.info(f"--- RAW AI RESPONSE ---\n{response_text}\n-----------------------")

        cleaned_text = response_text.strip()
        if cleaned_text.startswith('```json'): 
            cleaned_text = cleaned_text[7:]
        if cleaned_text.endswith('
```'): 
            cleaned_text = cleaned_text[:-3]
        cleaned_text = cleaned_text.strip()
            
        try:
            parsed_data = json.loads(cleaned_text)
            
            if isinstance(parsed_data, list) and len(parsed_data) > 0:
                result = {
                    "type": parsed_data[0].get("type", "open_dish"),
                    "status": parsed_data[0].get("status", "success"),
                    "dish_name": " + ".join([item.get("dish_name", "Dish") for item in parsed_data]),
                    "calories": sum([item.get("calories", 0) for item in parsed_data]),
                    "protein": round(sum([item.get("protein", 0) for item in parsed_data]), 1),
                    "fat": round(sum([item.get("fat", 0) for item in parsed_data]), 1),
                    "carbs": round(sum([item.get("carbs", 0) for item in parsed_data]), 1),
                    "scale_method": parsed_data[0].get("scale_method", "visual")
                }
            else:
                result = parsed_data
                
        except json.JSONDecodeError as json_err:
            logging.error(f"JSON Parse Error: {json_err}. Text: {response_text}")
            await status_msg.edit_text("⚠️ AI generated an invalid format. The issue has been logged, please try sending the photo again.")
            return

        if result.get("type") == "barcode" and result.get("barcode_digits"):
            barcode = str(result.get("barcode_digits")).strip()
            prod_name = result.get("dish_name", "Product")
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json") as resp:
                    if resp.status == 200:
                        api_data = await resp.json()
                        if api_data.get("status") == 1:
                            product = api_data.get("product", {})
                            prod_name = product.get("product_name_en") or product.get("product_name") or prod_name
                            nutriments = product.get("nutriments", {})
                            
                            kcal_100 = float(nutriments.get("energy-kcal_100g") or 0)
                            p_100 = float(nutriments.get("proteins_100g") or 0)
                            f_100 = float(nutriments.get("fat_100g") or 0)
                            c_100 = float(nutriments.get("carbohydrates_100g") or 0)
                            
                            if kcal_100 == 0:
                                await status_msg.edit_text(f"🏪 Product '{prod_name}' found, but macro metrics are missing from the global database.\nConsulting AI for an expert estimation...")
                                fallback_prompt = f"Generate average energy and macro metrics for the product '{prod_name}' per 100 grams. Return ONLY JSON structure: {{\"calories\": number, \"protein\": number, \"fat\": number, \"carbs\": number}}"
                                fb_resp = get_ai_response(fallback_prompt)
                                if fb_resp.text:
                                    try:
                                        fb_text_clean = fb_resp.text.strip()
                                        if fb_text_clean.startswith('```json'): 
                                            fb_text_clean = fb_text_clean[7:]
                                        if fb_text_clean.endswith('
```'): 
                                            fb_text_clean = fb_text_clean[:-3]
                                        fb_res = json.loads(fb_text_clean.strip())
                                        kcal_100 = float(fb_res.get("calories", 0))
                                        p_100 = float(fb_res.get("protein", 0))
                                        f_100 = float(fb_res.get("fat", 0))
                                        c_100 = float(fb_res.get("carbs", 0))
                                    except Exception:
                                        pass

                            await state.update_data(bc_name=prod_name, bc_kcal=kcal_100, bc_p=p_100, bc_f=f_100, bc_c=c_100)
                            await state.set_state(CorrectionStates.waiting_for_product_weight)
                            await status_msg.edit_text(
                                f"🏪 Product identified in global database!\n📦 Name: {prod_name}\n\n"
                                f"📊 Nutrition Value (per 100g):\n• {kcal_100} kcal\n• Macros: P: {p_100}g | F: {f_100}g | C: {c_100}g\n\n"
                                f"⚖️ Please reply with the weight in grams that you consumed (e.g., 150):"
                            )
                            return

        if result.get("status") == "no_palm":
            await status_msg.edit_text(
                "⚠️ Scaling Reference Error!\n\n"
                "I detected an open dish, but your palm was not found nearby. Without a scaling guide, I cannot estimate portion size.\n"
                "📸 Please take a new photo ensuring your hand is visible next to the dish."
            )
            return

        dish_name = result.get("dish_name", "Unknown Dish")
        calories = float(result.get("calories", 0))
        protein = float(result.get("protein") or 0.0)
        fat = float(result.get("fat") or 0.0)
        carbs = float(result.get("carbs") or 0.0)
        
        await state.update_data(pending_dish=dish_name, pending_cal=calories, pending_p=protein, pending_f=fat, pending_c=carbs)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Correct, save log", callback_data="confirm_food")],
            [InlineKeyboardButton(text="✏️ Edit details / parameters", callback_data="correct_food")]
        ])

        scale_method = result.get("scale_method", "")
        if scale_method == "standard_weight" or result.get("type") == "food":
            scale_text = "(calculated from retail standard weight configurations)"
        elif scale_method == "palm":
            scale_text = f"(calibrated using your {user_row[0]} cm palm size template)"
        else:
            scale_text = "(estimated via automated visual volume calculation)"

        await status_msg.edit_text(
            f"🍽️ Meal Analysis Complete!\n\n"
            f"🥗 Identified: {dish_name}\n"
            f"📊 Estimated Values {scale_text}:\n"
            f"🔥 Calories: ~{calories} kcal\n"
            f"🧬 Macros: Protein: {protein}g | Fat: {fat}g | Carbs: {carbs}g\n\n"
            f"Would you like to log this item into your food journal?", 
            reply_markup=kb
        )

    except RetryError:
        await status_msg.edit_text("⚠️ Google AI services are experiencing exceptionally high traffic. Please wait a moment and try submitting your photo again.")
    except Exception as e:
        logging.exception("Detailed error in process_photo_with_date:")
        if "503" in str(e):
            await status_msg.edit_text("⚠️ AI infrastructure is temporarily unavailable. Please try again shortly.")
        else:
            await status_msg.edit_text("⚠️ A system anomaly occurred while processing the image. Server diagnostics have been recorded.")

@dp.message(CorrectionStates.waiting_for_product_weight)
async def process_product_weight_input(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    try:
        wg = float(message.text.replace(",", "."))
        ud = await state.get_data()
        r = wg / 100.0
        target_date = ud.get('target_date', datetime.now().strftime("%Y-%m-%d"))
        
        with get_db() as conn:
            conn.cursor().execute(
                'INSERT INTO food_logs (user_id, date_time, dish_name, calories, protein, fat, carbs, water_ml, is_burn) VALUES (?,?,?,?,?,?,?,0,0)', 
                (message.from_user.id, f"{target_date} {datetime.now().strftime('%H:%M:%S')}", 
                 f"{ud.get('bc_name')} ({int(wg)}g)", 
                 round(ud.get('bc_kcal',0)*r), round(ud.get('bc_p',0)*r,1), round(ud.get('bc_f',0)*r,1), round(ud.get('bc_c',0)*r,1))
            )
        await message.answer("✅ Item weighed and logged to your daily journal successfully!", reply_markup=get_main_menu())
        await state.clear()
    except ValueError:
        await message.answer("⚠️ Please enter weight as a clean numerical value (e.g., 150).")

@dp.callback_query(F.data == "correct_food")
async def callback_correct_food(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not isinstance(callback.message, types.Message):
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(CorrectionStates.waiting_for_text)
    await callback.message.answer("✏️ Provide your correction adjustments (e.g., 'it is 184 calories per 100g, I ate 70g' or 'I only ate half of this portion'):")
    await callback.answer()

@dp.message(CorrectionStates.waiting_for_text)
async def process_food_correction(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    ud = await state.get_data()
    status_msg = await message.answer("🧠 AI recalculating configuration based on your refinement inputs...")
    
    try:
        photo_id = ud.get("temp_photo_file_id")
        if not photo_id or not isinstance(photo_id, str):
            return
        
        file_info = await bot.get_file(photo_id)
        photo_bytes = await bot.download_file(file_info.file_path)
        if photo_bytes is None:
            return
        image_data = photo_bytes.read()
        
        prompt = f"""
        You are an AI dietitian. The meal was previously classified as: {ud.get('pending_dish')}.
        Original values before adjustment: Calories: {ud.get('pending_cal')}, Protein: {ud.get('pending_p')}, Fat: {ud.get('pending_f')}, Carbs: {ud.get('pending_c')}.
        The user has supplied this correction instruction: '{message.text}'. All output parameters inside the JSON string must be strictly in English language.
        
        ATTENTION:
        1. If the user explicitly states specific values 'per 100 grams' along with the consumed portion weight, you MUST mathematically compute the final total macros for that custom portion (e.g., 184 kcal/100g * 0.7 = 128.8 kcal).
        2. If the user modifies quantities without giving explicit macro numbers, update macronutrients proportionally. Macro properties must NEVER be null or None. Always fallback to 0.0 if zero.
        
        Return ONLY a JSON string mapping absolute updated totals for the entire actual portion: {{"dish_name": "Dish Name", "calories": number, "protein": number, "fat": number, "carbs": number}}.
        """
        
        response = get_ai_response(prompt, image_data)
        response_text = response.text
        if not response_text:
            return
        
        cleaned_text = response_text.strip()
        if cleaned_text.startswith('```json'): 
            cleaned_text = cleaned_text[7:]
        if cleaned_text.endswith('
```'): 
            cleaned_text = cleaned_text[:-3]
        
        parsed_data = json.loads(cleaned_text.strip())
        
        if isinstance(parsed_data, list) and len(parsed_data) > 0:
            res = {
                "dish_name": " + ".join([item.get("dish_name", "Dish") for item in parsed_data]),
                "calories": sum([item.get("calories", 0) for item in parsed_data]),
                "protein": round(sum([item.get("protein") or 0.0 for item in parsed_data]), 1),
                "fat": round(sum([item.get("fat") or 0.0 for item in parsed_data]), 1),
                "carbs": round(sum([item.get("carbs") or 0.0 for item in parsed_data]), 1),
            }
        else:
            res = parsed_data
            
        protein = float(res.get("protein") or 0.0)
        fat = float(res.get("fat") or 0.0)
        carbs = float(res.get("carbs") or 0.0)
        
        await state.update_data(
            pending_dish=res.get("dish_name"), 
            pending_cal=res.get("calories"), 
            pending_p=protein, 
            pending_f=fat, 
            pending_c=carbs
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Correct, save log", callback_data="confirm_food")],
            [InlineKeyboardButton(text="✏️ Edit details / parameters", callback_data="correct_food")]
        ])
        
        await status_msg.edit_text(
            f"✅ Metrics updated successfully!\n\n"
            f"🍽️ Adjusted Meal: {res.get('dish_name')}\n"
            f"🔥 Calories: ~{res.get('calories')} kcal\n"
            f"🧬 Macros: Protein: {protein}g | Fat: {fat}g | Carbs: {carbs}g\n\n"
            f"Would you like to log this item into your food journal?",
            reply_markup=kb
        )
    except Exception as e:
        logging.exception("Error in process_food_correction")
        await status_msg.edit_text("⚠️ Failed to apply correction metrics. Please try again.")
        await state.clear()

@dp.callback_query(F.data == "confirm_food")
async def callback_confirm_food(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not isinstance(callback.message, types.Message) or not callback.from_user:
        return
        
    ud = await state.get_data()
    user_id = callback.from_user.id
    
    with get_db() as conn:
        conn.cursor().execute(
            'INSERT INTO food_logs (user_id, date_time, dish_name, calories, protein, fat, carbs, water_ml, is_burn) VALUES (?,?,?,?,?,?,?,0,0)', 
            (user_id, f"{ud.get('target_date')} {datetime.now().strftime('%H:%M:%S')}", ud.get("pending_dish"), ud.get("pending_cal"), ud.get("pending_p"), ud.get("pending_f"), ud.get("pending_c"))
        )
        
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Logged: **{ud.get('pending_dish')}** added to your daily tracker journal!", reply_markup=get_main_menu())
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("addwater_"))
async def callback_add_water(callback: types.CallbackQuery):
    if not callback.data or not callback.message or not isinstance(callback.message, types.Message) or not callback.from_user:
        return
        
    water_amount = int(callback.data.split("_")[1])
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with get_db() as conn:
        conn.cursor().execute('INSERT INTO food_logs (user_id, date_time, dish_name, calories, protein, fat, carbs, water_ml, is_burn) VALUES (?, ?, ?, 0, 0, 0, 0, ?, 0)', (callback.from_user.id, today_str, f"Pure Water (+{water_amount}ml)", water_amount))
        
    await callback.answer(f"💧 Added {water_amount} ml!")
    await callback.message.delete()
    await callback.message.answer(f"✅ Tracked! A volume of {water_amount} ml pure water has been recorded in your hydration balance.", reply_markup=get_main_menu())

@dp.message(F.text == "💧 Log Water")
async def cmd_water_menu(message: types.Message):
    if not message.from_user or is_throttled(message.from_user.id):
        return
    water_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥛 250 ml", callback_data="addwater_250"), InlineKeyboardButton(text="🍼 500 ml", callback_data="addwater_500"), InlineKeyboardButton(text="🐳 1000 ml", callback_data="addwater_1000")]
    ])
    await message.answer("💧 **Hydration Tracking Menu:**\n\nSelect the consumed fluid volume to update your balance metrics:", reply_markup=water_kb)

@dp.message(F.text == "🔥 Log Activity")
async def cmd_activity_start(message: types.Message, state: FSMContext):
    if not message.from_user or is_throttled(message.from_user.id):
        return
    await state.set_state(ActivityStates.waiting_for_activity_text)
    await message.answer("🏃 **Calorie Expenditure Tracking:**\n\nDescribe your physical activity using natural phrase phrasing (e.g., *'I ran 5 km in 30 minutes'* or *'One hour of yoga training'*).\n\nAI will parse your biometric context data to calculate real energetic cost parameters.", reply_markup=ReplyKeyboardRemove())

@dp.message(ActivityStates.waiting_for_activity_text)
async def process_activity_text_ai(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
        
    user_id = message.from_user.id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT weight, age, gender FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        
    if not user_row:
        await state.clear()
        return
    
    await message.answer("⚡ AI calculating energy expenditure specifications...")
    
    try:
        prompt = f"Calculate burned calories. Active context description: '{message.text}'. Weight: {user_row[0]}, Age: {user_row[1]}, Gender: {user_row[2]}. Return values inside single JSON object block matching this template in English: {{'activity_name': 'activity name in English', 'burned_calories': number}}"
        response = get_ai_response(prompt)
        response_text = response.text
        if not response_text:
            return
            
        res = json.loads(re.search(r'\{.*\}', response_text, re.DOTALL).group(0)) # type: ignore
        today_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with get_db() as conn:
            conn.cursor().execute('INSERT INTO food_logs (user_id, date_time, dish_name, calories, protein, fat, carbs, water_ml, is_burn) VALUES (?, ?, ?, ?, 0, 0, 0, 0, 1)', (user_id, today_str, f"🔥 Workout: {res.get('activity_name')}", int(res.get('burned_calories', 0))))
            
        await message.answer(f"✅ Activity recorded!\n\n🏃 Type: {res.get('activity_name')}\n🔥 Burned: -{res.get('burned_calories')} kcal\n\nThese expenditure values have been factored into your net daily balance.", reply_markup=get_main_menu())
        await state.clear()
    except Exception:
        await message.answer("⚠️ Analysis error encountered.", reply_markup=get_main_menu())
        await state.clear()

@dp.message(F.text == "🔔 My Reminders")
async def cmd_reminders_menu(message: types.Message):
    if not message.from_user or is_throttled(message.from_user.id):
        return
        
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT reminder_id, remind_time, remind_text FROM reminders WHERE user_id = ?", (message.from_user.id,))
        rows = cursor.fetchall()
        
    kb_list = [[InlineKeyboardButton(text="➕ Add New Reminder", callback_data="add_new_reminder")]]
    text_list = []
    
    for r in rows:
        text_list.append(f"⏰ {r[1]} — {r[2]}")
        kb_list.append([InlineKeyboardButton(text=f"🗑️ Delete reminder scheduled for {r[1]}", callback_data=f"delrem_{r[0]}")])
        
    await message.answer(
        f"📋 **Your Active Alert Notifications:**\n\n" + 
        ("\n".join(text_list) if text_list else "You have no active alert reminders configured yet.") + 
        "\n\n🔔 I will broadcast system prompts to your chat space directly at the selected timestamps.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list)
    )

@dp.callback_query(F.data == "add_new_reminder")
async def cb_add_reminder(callback: types.CallbackQuery, state: FSMContext):
    if not callback.message or not isinstance(callback.message, types.Message):
        return
    await state.set_state(ReminderStates.waiting_for_time)
    await callback.message.answer("🕰️ Specify time trigger target matching standard HH:MM configuration (e.g., 09:00):", reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@dp.message(ReminderStates.waiting_for_time)
async def process_remind_time(message: types.Message, state: FSMContext):
    if not message.text or not re.match(r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$", message.text.strip()): 
        await message.answer("⚠️ Format mismatch. Enter structural coordinates matching standard parameters like 14:30.")
        return
    await state.update_data(remind_time=message.text.strip())
    await message.answer("✍️ Excellent. Now type the alert description content (e.g., 'Time to consume protein whey shake!'):")
    await state.set_state(ReminderStates.waiting_for_text)

@dp.message(ReminderStates.waiting_for_text)
async def process_remind_text(message: types.Message, state: FSMContext):
    if not message.text or not message.from_user:
        return
    ud = await state.get_data()
    with get_db() as conn:
        conn.cursor().execute("INSERT INTO reminders (user_id, remind_time, remind_text) VALUES (?, ?, ?)", (message.from_user.id, ud.get('remind_time'), message.text))
    await message.answer(f"✅ Reminder alert created! Notice scheduled for execution at {ud.get('remind_time')}.", reply_markup=get_main_menu())
    await state.clear()

@dp.callback_query(F.data.startswith("delrem_"))
async def cb_delete_reminder(callback: types.CallbackQuery):
    if not callback.data or not callback.message or not isinstance(callback.message, types.Message):
        return
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM reminders WHERE reminder_id = ?", (callback.data.split("_")[1],))
    await callback.answer("🗑️ Reminder deleted!")
    await callback.message.delete()

async def check_reminders_job():
    current_time_str = datetime.now().strftime("%H:%M")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, remind_text FROM reminders WHERE remind_time = ?", (current_time_str,))
        active = cursor.fetchall()
    for user_id, text in active:
        try:
            await bot.send_message(chat_id=user_id, text=f"🔔 **SYSTEM ALERT REMINDER:**\n\n📢 {text}")
        except Exception:
            pass

@dp.message(F.text == "📊 Nutrition Stats")
async def cmd_stats_menu(message: types.Message):
    if not message.from_user or is_throttled(message.from_user.id):
        return
    t = datetime.now().strftime("%Y-%m-%d")
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ View Yesterday", callback_data=f"viewstats_{y}"), InlineKeyboardButton(text="🍏 Today", callback_data=f"viewstats_{t}")]
    ])
    await message.answer("📊 Select target frame to load nutrition tracking metrics metrics summary:", reply_markup=kb)

@dp.callback_query(F.data.startswith("viewstats_"))
async def process_view_stats(callback: types.CallbackQuery):
    if not callback.message or not callback.data or not isinstance(callback.message, types.Message) or not callback.from_user:
        return
        
    dt = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT daily_norm FROM users WHERE user_id = ?", (user_id,))
        user_norm = cursor.fetchone()
        daily_norm = user_norm[0] if user_norm else 2000
        cursor.execute('SELECT dish_name, calories, protein, fat, carbs, water_ml, is_burn FROM food_logs WHERE user_id = ? AND date_time LIKE ?', (user_id, f"{dt}%"))
        logs = cursor.fetchall()
    
    await callback.message.delete()
    if not logs: 
        await callback.message.answer(f"📅 Journal space data for {dt}:\nNo entries located. Your daily maintenance allowance is: {daily_norm} kcal.")
        await callback.answer()
        return
    
    food_list, total_cal, total_p, total_f, total_c, total_water, total_burned = [], 0.0, 0.0, 0.0, 0.0, 0, 0
    
    for l in logs:
        if l[5] > 0:
            total_water += l[5]
        elif l[6] == 1:
            total_burned += l[1]
            food_list.append(f"{l[0]} (-{int(l[1])} kcal) 🏃")
        else:
            food_list.append(f"• {l[0]} (+{int(l[1])} kcal)")
            total_cal += l[1]
            total_p += l[2]
            total_f += l[3]
            total_c += l[4]
    
    fl_text = "\n".join(food_list) if food_list else "• No recorded food items located."
    final_balance = total_cal - total_burned
    remains = daily_norm - final_balance
    status_msg = f"🟢 Intake remaining allowance: {int(remains)} kcal" if remains >= 0 else f"🔴 Limit exceeded by {int(abs(remains))} kcal!"
    water_percentage = min(round((total_water / 2000) * 100), 100)
    
    caption_text = (
        f"📅 **Your Tracking History for {dt}:**\n\n{fl_text}\n\n"
        f"💧 Hydration Level: {total_water} / 2000 ml ({water_percentage}%)\n\n"
        f"--- SUMMARY STATISTICS ---\n"
        f"📥 Food Consumed: {int(total_cal)} kcal\n"
        f"🏃 Energy Expenditure: {int(total_burned)} kcal\n"
        f"🔥 Net Balance: {int(final_balance)} / {daily_norm} kcal\n"
        f"💡 {status_msg}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Delete Last Log Entry", callback_data=f"undo_{dt}")]
    ])
    
    await callback.message.answer(caption_text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("undo_"))
async def callback_undo_last_log(callback: types.CallbackQuery):
    if not callback.message or not callback.data or not isinstance(callback.message, types.Message) or not callback.from_user:
        return
        
    dt = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT log_id, dish_name FROM food_logs WHERE user_id = ? AND date_time LIKE ? ORDER BY log_id DESC LIMIT 1", (user_id, f"{dt}%"))
        last_log = cursor.fetchone()
        
        if last_log:
            cursor.execute("DELETE FROM food_logs WHERE log_id = ?", (last_log[0],))
            conn.commit()
            await callback.answer(f"🗑 Entry '{last_log[1]}' successfully removed!")
            await callback.message.delete()
            await callback.message.answer(f"✅ Last logged event for {dt} erased. Please reload stats parameters to view updated metrics.", reply_markup=get_main_menu())
        else:
            await callback.answer("⚠️ No trackable entries located for this timeline frame.")

@dp.message(F.text == "📈 Weight Progress")
async def cmd_weight_chart(message: types.Message):
    if not message.from_user or is_throttled(message.from_user.id):
        return
        
    user_id = message.from_user.id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT date_str, weight_real FROM weight_history WHERE user_id = ? ORDER BY date_str ASC", (user_id,))
        rows = cursor.fetchall()
        
    if len(rows) < 2: 
        await message.answer("📈 **Progress Analytics:**\n\nA graphical trendline require at least two distinct weight entry logs logged across separate dates.")
        return
        
    dates = [datetime.strptime(r[0], "%Y-%m-%d").strftime("%d.%m") for r in rows]
    weights = [float(r[1]) for r in rows]
    
    await message.answer("📊 Processing metric tracking matrix and plotting your progress trendline...")
    try:
        plt.figure(num=user_id + 10000, figsize=(8, 4.5))
        plt.clf()
        plt.plot(dates, weights, marker='o', color='#2b7de9', linestyle='-', linewidth=2, markersize=6)
        for i, w in enumerate(weights):
            plt.annotate(f"{w} kg", (i, w), textcoords="offset points", xytext=(0,8), ha='center', fontsize=9, fontweight='bold')
        plt.title("📈 Body Weight Progression Chart", fontsize=12, fontweight='bold', pad=15)
        plt.grid(True, linestyle='--')
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plt.close()
        
        await message.answer_photo(photo=BufferedInputFile(buf.read(), filename="weight.png"), caption="📉 Your personalized graphical weight progress chart overview!")
    except Exception: 
        await message.answer("⚠️ Failed to build progression analytics representation.")

@dp.message(F.text == "🚨 Reset All")
async def cmd_reset_click(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💥 YES, erase profile data", callback_data="confirm_full_reset"), InlineKeyboardButton(text="❌ No, cancel", callback_data="cancel_reset")]
    ])
    await message.answer("⚠️ ATTENTION CRITICAL ACTION! Are you absolutely certain you want to permanently erase your profile, logging histories, and data entries from the system servers?", reply_markup=kb)

@dp.callback_query(F.data == "confirm_full_reset")
async def callback_confirm_reset(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user or not callback.message or not isinstance(callback.message, types.Message):
        return
        
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE user_id = ?", (callback.from_user.id,))
        cursor.execute("DELETE FROM food_logs WHERE user_id = ?", (callback.from_user.id,))
        cursor.execute("DELETE FROM reminders WHERE user_id = ?", (callback.from_user.id,))
        cursor.execute("DELETE FROM weight_history WHERE user_id = ?", (callback.from_user.id,))
        cursor.execute("DELETE FROM allowed_users WHERE user_id = ?", (callback.from_user.id,))
        
    await callback.message.delete()
    await callback.message.answer("🗑️ All your personal configuration datasets have been wiped from system storage. Call /start to initialize a new user profile setup.", reply_markup=ReplyKeyboardRemove())
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "cancel_reset")
async def callback_cancel_reset(callback: types.CallbackQuery):
    if not callback.message or not isinstance(callback.message, types.Message):
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Reset operation aborted. Your datasets remain secure.", reply_markup=get_main_menu())
    await callback.answer()

@dp.message(F.document)
async def handle_food_document(message: types.Message):
    if message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        await message.answer("📎 Please transmit image parameters as standard **photo compressed objects** (with instant media view generation) instead of plain documents!")

async def main() -> None:
    init_db()
    
    scheduler.add_job(check_reminders_job, "interval", minutes=1)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())