# AI-Powered Smart Calorie Tracker Bot 

An advanced, asynchronous Telegram bot designed to completely automate personal nutrition and hydration tracking using computer vision and LLMs. Instead of manually searching for food metrics, logging macronutrients, or calculating calorie limits, users can manage their health metrics seamlessly inside Telegram through natural language and images.


## Core Features

*   **Computer Vision Nutrition Logging:** Users upload a photo of a meal next to their palm. The bot uses the predefined user's palm size as a dynamic spatial reference matrix to isolate volume scale and accurately extract absolute calorie, protein, fat, and carbohydrate parameters.
*   **Intelligent Barcode Scanning:** Integrated lookup querying the global OpenFoodFacts API via asynchronous HTTP protocols. If factory items lack nutritional values in the database, a fallback AI engine infers the standard per-100g nutritional matrix automatically.
*   **Natural Language Adjustments:** Users can refine logs using human phrasing (e.g., *"I only ate half of this portion"* or *"This item contains 200 kcal per 100g and I had 80g"*). The AI parses the structural data context, processes the math, and updates metrics dynamically.
*   **Dynamic Data Visualization:** Automatically renders weight trends over historical timeline points using non-GUI Matplotlib pipelines, streaming visual updates straight into compressed memory buffers (`io.BytesIO`) without local file clutter.
*   **Automated Chron Jobs:** Embedded scheduling loops executing asynchronous polling routines to safely distribute timed system alert push notifications to target user chats.


## Architecture & Technical Highlights

*   **Asynchronous Processing Pipeline:** Built on top of `aiogram v3` and `asyncio`, utilizing non-blocking polling mechanics capable of handling concurrent tasks and high user throughput seamlessly.
*   **Structured AI Outputs:** Integrates the Google GenAI SDK (`gemini-2.5-flash`) enforcing a rigid schema constraint prompt template that guarantees strict JSON structural mapping for error-free parsing.
*   **Thread-Safe State & Resource Isolation:** Leverages AIogram FSM (Finite State Machine) architectures for registration, security, and item tracking operations. Database connections are handled using dedicated Python context managers (`get_db`) ensuring automatic transaction handling and elimination of file descriptor leaks.
*   **Resilience & Protection Middlewares:** Implemented customized time-based sliding scale throttle rate limits to protect infrastructure from abuse, and integrated advanced exponential backoff retry policies (`tenacity`) to guarantee system stability against third-party API outages.


## 📦 Tech Stack

*   **Language:** Python 3.10+
*   **Bot Framework:** `aiogram v3` (Asynchronous Telegram Bots API)
*   **AI Engine:** Google Gemini API (`google-genai`)
*   **Database Engine:** SQLite3 (Persistent relational data storage)
*   **Network Operations:** `aiohttp` (Asynchronous HTTP Client)
*   **Data Visualization:** `matplotlib` (Automated backend chart plotting)
*   **Task Scheduling:** `apscheduler` (Asynchronous event scheduling runner)
*   **Fault Tolerance:** `tenacity` (Advanced retry behavior wrappers)

