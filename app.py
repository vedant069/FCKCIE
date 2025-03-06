import os
import re
import json
import time
import streamlit as st
import base64
from io import BytesIO
from dotenv import load_dotenv
from google import genai
import subprocess
import sys

# Import Playwright instead of pyppeteer
from playwright.sync_api import sync_playwright

# Load environment variables from .env
load_dotenv()
GEMINI_API_KEY = 'AIzaSyAOK9vRTSRQzd22B2gmbiuIePbZTDyaGYs'

# --- Browser Setup ---
def install_playwright_browsers():
    """Install Playwright browsers if they're not already installed"""
    try:
        st.info("Installing Playwright browsers...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            check=True
        )
        st.success("Playwright browsers installed successfully!")
        return True
    except subprocess.CalledProcessError as e:
        st.error(f"Failed to install Playwright browsers: {e.stderr}")
        return False
    except Exception as e:
        st.error(f"Unexpected error installing browsers: {e}")
        return False

def setup_browser():
    """Set up a Playwright browser instance"""
    try:
        st.info("Initializing headless browser...")
        
        # Make sure browsers are installed
        if "playwright_browsers_installed" not in st.session_state:
            install_playwright_browsers()
            st.session_state.playwright_browsers_installed = True
        
        # Start Playwright
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ]
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800}
        )
        page = context.new_page()
        return {"playwright": playwright, "browser": browser, "context": context, "page": page}
    except Exception as e:
        st.error(f"Failed to initialize browser: {e}")
        raise

# --- Utility Functions ---
def take_screenshot(browser_data):
    """Takes a screenshot using Playwright"""
    try:
        page = browser_data["page"]
        screenshot_bytes = page.screenshot()
        return screenshot_bytes
    except Exception as e:
        st.error(f"Screenshot error: {e}")
        return None

def extract_questions_from_fb_data(html):
    """
    Parses the rendered HTML to extract questions and options from the
    FB_PUBLIC_LOAD_DATA_ JavaScript variable.
    """
    match = re.search(r'var\s+FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);</script>', html, re.DOTALL)
    if not match:
        st.error("FB_PUBLIC_LOAD_DATA_ not found in HTML.")
        return []
    raw_json = match.group(1)
    # Replace common escaped sequences for valid JSON
    replacements = {
        r'\\n': '\n',
        r'\\u003c': '<',
        r'\\u003e': '>',
        r'\\u0026': '&',
        r'\\"': '"'
    }
    for old, new in replacements.items():
        raw_json = raw_json.replace(old, new)
    raw_json = re.sub(r'[\x00-\x08\x0B-\x1F\x7F]', '', raw_json)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        st.error(f"Error decoding FB_PUBLIC_LOAD_DATA_ JSON: {e}")
        return []
    
    # Typically, questions are stored in data[1][1]
    questions = []
    try:
        questions_data = data[1][1]
    except (IndexError, TypeError):
        return questions

    for item in questions_data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        q_text = item[1] if isinstance(item[1], str) else None
        if not q_text:
            continue
        q_text = q_text.strip()
        # For multiple-choice questions, options usually appear in item[4]
        choices = []
        if len(item) > 4 and isinstance(item[4], list):
            for block in item[4]:
                if isinstance(block, list) and len(block) > 1 and isinstance(block[1], list):
                    for opt in block[1]:
                        if isinstance(opt, list) and len(opt) > 0 and isinstance(opt[0], str):
                            choices.append(opt[0])
        questions.append({
            "question_text": q_text,
            "options": choices
        })
    return questions

def generate_answers(questions, api_key):
    """
    For each question, call Google Gemini to generate an answer that matches available options.
    """
    client = genai.Client(api_key=api_key)
    for q in questions:
        question_text = q["question_text"]
        options = q["options"]
        
        if options:
            # For multiple choice questions, make prompt more specific to choose exactly one option
            prompt = f"""
            Question: {question_text}
            
            These are the EXACT options (choose only one):
            {', '.join([f'"{opt}"' for opt in options])}
            
            Instructions:
            1. Choose exactly ONE option from the list above
            2. Return ONLY the exact text of the chosen option, nothing else
            3. Do not add any explanation, just the option text
            4. Do not add quotation marks around the option
            5. Do not answer questions like "What is your name?","Rollno","PRN/GRN","Email","Mobile No","Address","DOB etc
            
            Answer:
            """
        else:
            # For free-text questions, keep it simple
            prompt = f"""
            Question: {question_text}
            
            Please provide a brief and direct answer to this question.
            Keep your answer concise (1-2 sentences maximum).
            
            Answer:
            """
        
        try:
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=prompt
            )
            
            answer = response.text.strip()
            
            # For multiple choice, ensure it exactly matches one of the options
            if options:
                exact_match = False
                for opt in options:
                    if opt.lower() == answer.lower():
                        answer = opt  # Use the exact casing from the original option
                        exact_match = True
                        break
                
                # If no exact match, use the most similar option
                if not exact_match:
                    from difflib import SequenceMatcher
                    best_match = max(options, key=lambda opt: SequenceMatcher(None, opt.lower(), answer.lower()).ratio())
                    answer = best_match
            
            q["gemini_answer"] = answer
            
        except Exception as e:
            q["gemini_answer"] = f"Error: {str(e)}"
            
    return questions

def fill_form(browser_data, questions):
    """Fills the Google Form with generated answers using Playwright"""
    page = browser_data["page"]
    
    # Locate question containers
    question_containers = page.query_selector_all('div.freebirdFormviewerViewItemsItemItem, div[role="listitem"]')
    
    if not question_containers:
        st.error("Could not locate question containers in the form.")
        return False
        
    # Print total questions found for debugging
    print(f"Found {len(question_containers)} question containers in the form")
    print(f"We have {len(questions)} questions with answers to fill")

    # Give the form time to fully render
    time.sleep(2)

    for idx, q in enumerate(questions):
        if idx >= len(question_containers):
            break
            
        print(f"\n--------- Processing Question {idx+1} ---------")
        container = question_containers[idx]
        answer = q.get("gemini_answer", "").strip()
        options = q.get("options", [])
        
        # Print question and answer for debugging
        print(f"Question: {q['question_text']}")
        print(f"Generated Answer: {answer}")
        
        if options:
            try:
                print(f"This is a multiple-choice question with {len(options)} options")
                
                # Try multiple selector strategies to find radio buttons or checkboxes
                option_elements = container.query_selector_all('div[role="radio"], label, div.appsMaterialWizToggleRadiogroupRadioButtonContainer, .docssharedWizToggleLabeledLabelWrapper')
                
                if not option_elements:
                    st.warning(f"Could not find option elements for question {idx+1}")
                    print("No option elements found with any selector strategy")
                    continue
                
                # Normalize the answer text to make matching more robust
                import re
                normalized_answer = re.sub(r'[^\w\s]', '', answer.lower()).strip()
                
                # First pass: Try exact matches
                clicked = False
                print("\nTrying exact matches...")
                
                # Try to match element text with our answer
                for i, opt_elem in enumerate(option_elements):
                    # Try to get the text content of the option
                    opt_text = opt_elem.inner_text() or ""
                    opt_text = opt_text.strip()
                    
                    # If no text found, try to get aria-label
                    if not opt_text:
                        opt_text = opt_elem.get_attribute("aria-label") or ""
                        opt_text = opt_text.strip()
                    
                    # Normalize option text
                    normalized_opt = re.sub(r'[^\w\s]', '', opt_text.lower()).strip()
                    
                    # Try exact match
                    if normalized_opt and normalized_opt == normalized_answer:
                        opt_elem.click()
                        clicked = True
                        print(f"Clicked option: '{opt_text}' (exact match)")
                        break
                
                # If no exact match found, try substring matching
                if not clicked:
                    for i, opt_elem in enumerate(option_elements):
                        opt_text = opt_elem.inner_text() or opt_elem.get_attribute("aria-label") or ""
                        opt_text = opt_text.strip()
                        normalized_opt = re.sub(r'[^\w\s]', '', opt_text.lower()).strip()
                        
                        if normalized_opt and (normalized_opt in normalized_answer or normalized_answer in normalized_opt):
                            opt_elem.click()
                            clicked = True
                            print(f"Clicked option: '{opt_text}' (substring match)")
                            break
                
                # Last resort: click the first element as fallback
                if not clicked and option_elements:
                    option_elements[0].click()
                    print("No match found. Clicked first option as fallback")
                    
            except Exception as e:
                st.error(f"Error filling multiple-choice question {idx+1}: {e}")
                print(f"Exception: {str(e)}")
        else:
            try:
                print("This is a text question")
                # For text questions, locate the text input or textarea
                input_elem = container.query_selector('input[type="text"], textarea, input')
                
                if input_elem:
                    input_elem.fill(answer)
                    print(f"Filled text answer: {answer}")
                else:
                    st.error(f"Could not locate input element for question {idx+1}")
                    print("Failed to find any input element for this question")
                
            except Exception as e:
                st.error(f"Error filling text question {idx+1}: {e}")
                print(f"Exception: {str(e)}")
    
    print("\n---------- Form filling completed ----------")
    return True

def login_to_google(browser_data, email, password):
    """Logs into Google account using Playwright"""
    try:
        page = browser_data["page"]
        
        # Navigate to Google login page
        page.goto("https://accounts.google.com/signin")
        time.sleep(2)
        
        # Take screenshot to show the login page
        screenshot = take_screenshot(browser_data)
        st.image(screenshot, caption="Login Page", use_column_width=True)
        
        # Enter email
        email_input = page.wait_for_selector('input[type="email"]')
        email_input.fill(email)
        page.keyboard.press('Enter')
        time.sleep(2)
        
        # Take screenshot after email entry
        screenshot = take_screenshot(browser_data)
        st.image(screenshot, caption="Email Entered", use_column_width=True)
        
        # Enter password
        password_input = page.wait_for_selector('input[type="password"]')
        password_input.fill(password)
        page.keyboard.press('Enter')
        time.sleep(5)  # Wait for login to complete
        
        # Take screenshot after login attempt
        screenshot = take_screenshot(browser_data)
        st.image(screenshot, caption="Login Attempt Result", use_column_width=True)
        
        # Check if login was successful
        try:
            # Check if we're no longer on the accounts.google.com/signin page
            if "accounts.google.com/signin" not in page.url:
                return True
                
            # Check for possible 2FA prompt
            page_content = page.content()
            if "2-Step Verification" in page_content or "verification" in page_content.lower():
                st.warning("Two-factor authentication detected. Please complete it in the browser window.")
                return "2FA"
                
            return False
            
        except Exception:
            # If no clear indicators, assume successful if URL changed
            if "accounts.google.com/signin" not in page.url:
                return True
            return False
            
    except Exception as e:
        st.error(f"Error during login: {str(e)}")
        return False

# --- Streamlit App ---
st.title("Google Form Auto Filler with Gemini")
st.write("""
This app uses a headless browser to help you fill Google Forms automatically with AI-generated answers.
You'll be able to see screenshots of what's happening in the browser as it progresses.
""")

# Initialize session state variables
if "browser_data" not in st.session_state:
    st.session_state.browser_data = None
if "login_status" not in st.session_state:
    st.session_state.login_status = None
if "form_filled" not in st.session_state:
    st.session_state.form_filled = False
if "screenshot" not in st.session_state:
    st.session_state.screenshot = None

# Step 1: Login to Google Account
st.header("Step 1: Login to Google Account")

# Collect Google credentials
with st.form("google_login"):
    email = st.text_input("Google Email")
    password = st.text_input("Google Password", type="password")
    submit_button = st.form_submit_button("Login to Google")
    
    if submit_button and email and password:
        # Initialize browser
        try:
            browser_data = setup_browser()
            st.session_state.browser_data = browser_data
            
            # Show initial browser window
            screenshot = take_screenshot(browser_data)
            st.session_state.screenshot = screenshot
            st.image(screenshot, caption="Browser Started", use_column_width=True)
            
            # Try to login
            login_result = login_to_google(browser_data, email, password)
            st.session_state.login_status = login_result
            
            if login_result == True:
                st.success("Login successful!")
            elif login_result == "2FA":
                st.warning("Two-factor authentication may be required. Check the screenshot for verification prompts.")
                st.info("You might need to complete 2FA in the browser window. Screenshots will update as you proceed.")
            else:
                st.error("Login failed. Please check your credentials and try again.")
        
        except Exception as e:
            st.error(f"Error initializing browser: {str(e)}")
            st.info("If you're seeing this error, please check the Streamlit Cloud logs for details.")
            
# Add manual confirmation option for login
if st.session_state.login_status == False:
    st.info("If you can see that you're actually logged in from the screenshot above, click the button below:")
    if st.button("I'm actually logged in successfully"):
        st.session_state.login_status = True
        st.success("Login status manually confirmed! You can proceed to the form filling step.")

# Display a refreshing screenshot if 2FA is detected
if st.session_state.login_status == "2FA" and st.session_state.browser_data:
    if st.button("Take New Screenshot (for 2FA completion check)"):
        screenshot = take_screenshot(st.session_state.browser_data)
        st.session_state.screenshot = screenshot
        st.image(screenshot, caption="Current Browser State", use_column_width=True)
        
        # Check if we're past the login page now
        page = st.session_state.browser_data["page"]
        current_url = page.url
        if "accounts.google.com/signin" not in current_url:
            st.success("Looks like you completed 2FA! You can proceed to the form filling step.")
            st.session_state.login_status = True

if st.session_state.browser_data and (st.session_state.login_status == True or st.session_state.login_status == "2FA"):
    st.header("Step 2: Fill Google Form")
    
    # Make this more prominent
    st.markdown("### Enter your Google Form URL below:")
    form_url = st.text_input("Form URL:", key="form_url_input")
    
    if form_url:
        # Store form URL in session state so we can access it later
        if "form_url" not in st.session_state:
            st.session_state.form_url = form_url
        
        # Process Form button
        if st.button("Process Form", key="process_form_button") or "questions" in st.session_state:
            browser_data = st.session_state.browser_data
            
            # Only load the form if questions aren't already processed
            if "questions" not in st.session_state:
                page = browser_data["page"]
                page.goto(form_url)
                time.sleep(5)  # Allow the form to load completely
                
                # Show the form
                screenshot = take_screenshot(browser_data)
                st.image(screenshot, caption="Google Form Loaded", use_column_width=True)
                
                html = page.content()
                
                # Extract questions from the form
                questions = extract_questions_from_fb_data(html)
                if not questions:
                    st.error("No questions extracted from the form.")
                else:
                    st.success(f"Successfully extracted {len(questions)} questions from the form.")
                    
                    # Generate answers using Google Gemini
                    with st.spinner("Generating answers with Gemini..."):
                        questions = generate_answers(questions, GEMINI_API_KEY)
                    
                    # Store questions in session state
                    st.session_state.questions = questions
            else:
                # Use the stored questions
                questions = st.session_state.questions
            
            # Display the questions and answers
            st.write("--- Generated Answers ---")
            for idx, q in enumerate(questions, start=1):
                st.write(f"**Question {idx}:** {q['question_text']}")
                if q["options"]:
                    st.write("Options:", ", ".join(q["options"]))
                else:
                    st.write("(No multiple-choice options)")
                st.write("**Generated Answer:**", q["gemini_answer"])
                st.write("---")
            
            # Add a clear separation before form actions
            st.markdown("### Form Actions")
            
            # Fill form button - only show if form not already filled
            if not st.session_state.get("form_filled", False):
                if st.button("Fill Form with Generated Answers", key="fill_form_button"):
                    with st.spinner("Filling form..."):
                        # Navigate to the form again to ensure clean state
                        page = browser_data["page"]
                        page.goto(st.session_state.form_url)
                        time.sleep(3)
                        
                        if fill_form(browser_data, questions):
                            time.sleep(2)  # Give time for all fields to be properly filled
                            
                            # Take screenshot after filling
                            filled_screenshot = take_screenshot(browser_data)
                            st.session_state.filled_screenshot = filled_screenshot
                            st.session_state.form_filled = True
                            
                            st.success("Form successfully filled with generated answers!")
                            st.image(filled_screenshot, caption="Form Filled with Answers", use_column_width=True)
            
            # Show the filled form if it exists in session state
            if st.session_state.get("form_filled", False) and "filled_screenshot" in st.session_state:
                if not st.session_state.get("showing_filled_form", False):
                    st.image(st.session_state.filled_screenshot, caption="Form Filled with Generated Answers", use_column_width=True)
                    st.session_state.showing_filled_form = True
                
                # Instruction message instead of submit button
                st.success("✅ Form has been filled with AI-generated answers! Just go and change your name and stuff")
                st.info("💡 You can check the answers generated by opening the form link on your browser.")
                st.markdown(f"📝 **Form Link:** [Open in Browser]({form_url})")

# Option to close the browser
if st.session_state.browser_data:
    st.markdown("---")
    if st.button("Close Browser"):
        try:
            # Properly close Playwright resources
            browser_data = st.session_state.browser_data
            browser_data["browser"].close()
            browser_data["playwright"].stop()
            
            # Clear session state
            st.session_state.browser_data = None
            st.session_state.login_status = None
            st.session_state.form_filled = False
            st.session_state.questions = None
            st.session_state.form_url = None
            st.session_state.filled_screenshot = None
            st.session_state.showing_filled_form = False
            st.success("Browser closed. All session data cleared.")
        except Exception as e:
            st.error(f"Error closing browser: {e}")