import asyncio
import nest_asyncio
import os
import re
import json
import time
import streamlit as st
import base64
from io import BytesIO
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from google import genai

# Load environment variables from .env
load_dotenv()
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    st.error("GEMINI_API_KEY environment variable not set. Please configure it properly.")

# Initialize asyncio for threaded environments
try:
    asyncio.get_event_loop()
except RuntimeError:
    # If there is no event loop in this thread, create one and make it current
    asyncio.set_event_loop(asyncio.new_event_loop())
    
# Apply nest_asyncio to allow nested event loops 
# (sometimes needed in Streamlit)
nest_asyncio.apply()
# --- Utility Functions ---

def take_screenshot(driver):
    """
    Takes a screenshot of the current browser window and returns it as an image
    that can be displayed in Streamlit.
    """
    screenshot = driver.get_screenshot_as_png()
    return screenshot

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
    try:
        # Ensure we have an event loop in this thread
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        client = genai.Client(api_key=api_key)
        
        for q in questions:
            question_text = q["question_text"]
            options = q["options"]
            
            # Rest of your existing function code...
            if options:
                prompt = f"""
                Question: {question_text}
                
                These are the EXACT options (choose only one):
                {', '.join([f'"{opt}"' for opt in options])}
                
                Instructions:
                1. Choose exactly ONE option from the list above
                2. Return ONLY the exact text of the chosen option, nothing else
                3. Do not add any explanation, just the option text
                4. Do not add quotation marks around the option
                5. Don not answer questions like "What is your name?","Rollno","PRN/GRN","Email","Mobile No","Address","DOB etc
                
                Answer:
                """
            else:
                prompt = f"""
                Question: {question_text}
                
                Please provide a brief and direct answer to this question.
                Keep your answer concise (1-2 sentences maximum).
                
                Answer:
                """
            
            try:
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
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
                st.error(f"Error generating answer: {str(e)}")
                
        return questions
        
    except Exception as e:
        st.error(f"Error in generate_answers function: {str(e)}")
        # Return questions with error messages
        for q in questions:
            if "gemini_answer" not in q:
                q["gemini_answer"] = f"Error: Could not generate answer due to {str(e)}"
        return questions

def fill_form(driver, questions):
    """
    Fills the Google Form with generated answers using the provided driver.
    """
    # Locate question containers (try different selectors)
    question_containers = driver.find_elements(By.CSS_SELECTOR, "div.freebirdFormviewerViewItemsItemItem")
    if not question_containers:
        question_containers = driver.find_elements(By.CSS_SELECTOR, "div[role='listitem']")
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
                option_elements = container.find_elements(By.CSS_SELECTOR, "div[role='radio']")
                if not option_elements:
                    option_elements = container.find_elements(By.CSS_SELECTOR, "label")
                if not option_elements:
                    option_elements = container.find_elements(By.CSS_SELECTOR, "div.appsMaterialWizToggleRadiogroupRadioButtonContainer")
                if not option_elements:
                    option_elements = container.find_elements(By.CSS_SELECTOR, ".docssharedWizToggleLabeledLabelWrapper")
                
                if not option_elements:
                    st.warning(f"Could not find option elements for question {idx+1}")
                    print("No option elements found with any selector strategy")
                    continue
                
                print(f"Found {len(option_elements)} option elements in the form")
                
                # Normalize the answer text to make matching more robust
                import re
                normalized_answer = re.sub(r'[^\w\s]', '', answer.lower()).strip()
                
                # First pass: Try exact matches
                clicked = False
                print("\nTrying exact matches...")
                
                # Create a dictionary mapping option text to elements
                option_dict = {}
                
                # First extract all option texts
                for i, opt_elem in enumerate(option_elements):
                    # Get text directly and from child elements if needed
                    opt_text = opt_elem.text.strip()
                    
                    # If no text, try getting from child elements
                    if not opt_text:
                        for child in opt_elem.find_elements(By.XPATH, ".//div"):
                            child_text = child.text.strip()
                            if child_text:
                                opt_text = child_text
                                break
                                
                    # Still no text? Try aria-label
                    if not opt_text:
                        opt_text = opt_elem.get_attribute("aria-label") or ""
                        
                    # Store in dictionary for later use if we have text
                    if opt_text:
                        normalized_opt = re.sub(r'[^\w\s]', '', opt_text.lower()).strip()
                        option_dict[normalized_opt] = opt_elem
                        print(f"Option {i+1}: '{opt_text}' (normalized: '{normalized_opt}')")
                    else:
                        print(f"Option {i+1}: [NO TEXT FOUND]")
                
                # Try exact match
                if normalized_answer in option_dict:
                    print(f"Found exact match for: '{normalized_answer}'")
                    option_dict[normalized_answer].click()
                    clicked = True
                else:
                    # Try substring matches
                    for opt_text, opt_elem in option_dict.items():
                        if opt_text in normalized_answer or normalized_answer in opt_text:
                            print(f"Found partial match: '{opt_text}' with answer '{normalized_answer}'")
                            opt_elem.click()
                            clicked = True
                            break
                
                # Try matching with original options
                if not clicked:
                    print("\nTrying to match with original options list...")
                    for i, original_opt in enumerate(options):
                        print(f"Original option {i+1}: '{original_opt}'")
                        normalized_orig = re.sub(r'[^\w\s]', '', original_opt.lower()).strip()
                        
                        # First check direct equality
                        if normalized_orig == normalized_answer:
                            print(f"EXACT match with original option: '{original_opt}'")
                            
                            # Find matching element in the dictionary or by position
                            if normalized_orig in option_dict:
                                option_dict[normalized_orig].click()
                                clicked = True
                                break
                            elif i < len(option_elements):
                                print(f"Clicking by position: element {i}")
                                option_elements[i].click()
                                clicked = True
                                break
                        
                        # Then try substring matching
                        elif normalized_orig in normalized_answer or normalized_answer in normalized_orig:
                            print(f"PARTIAL match with original option: '{original_opt}'")
                            if i < len(option_elements):
                                option_elements[i].click()
                                clicked = True
                                break
                
                # Try similarity matching as last resort
                if not clicked:
                    print("\nNo direct matches found, trying similarity matching...")
                    from difflib import SequenceMatcher
                    
                    # Try matching with form elements
                    best_score = 0
                    best_element = None
                    for opt_text, opt_elem in option_dict.items():
                        score = SequenceMatcher(None, opt_text, normalized_answer).ratio()
                        if score > best_score and score > 0.6:  # Require at least 60% similarity
                            best_score = score
                            best_element = opt_elem
                    
                    if best_element:
                        print(f"Best similarity match score: {best_score}")
                        best_element.click()
                        clicked = True
                    else:
                        # Try matching with original options
                        best_score = 0
                        best_idx = 0
                        for i, original_opt in enumerate(options):
                            normalized_orig = re.sub(r'[^\w\s]', '', original_opt.lower()).strip()
                            score = SequenceMatcher(None, normalized_orig, normalized_answer).ratio()
                            if score > best_score:
                                best_score = score
                                best_idx = i
                        
                        if best_score > 0.5 and best_idx < len(option_elements):  # 50% similarity threshold
                            print(f"Best similarity with original option: '{options[best_idx]}' (score: {best_score})")
                            option_elements[best_idx].click()
                            clicked = True
                
                # Last resort: click first option if nothing matched
                if not clicked and option_elements:
                    st.warning(f"No match found for question {idx+1}, selecting first option as fallback")
                    print("No suitable match found, clicking first option as fallback")
                    option_elements[0].click()
                    
            except Exception as e:
                st.error(f"Error filling multiple-choice question {idx+1}: {e}")
                print(f"Exception: {str(e)}")
        else:
            try:
                print("This is a text question")
                # For text questions, locate the text input or textarea and fill in the answer
                input_elem = None
                
                # Try multiple strategies to find the text input
                try:
                    input_elem = container.find_element(By.CSS_SELECTOR, "input[type='text']")
                    print("Found text input element")
                except Exception:
                    try:
                        input_elem = container.find_element(By.CSS_SELECTOR, "textarea")
                        print("Found textarea element")
                    except Exception:
                        try:
                            # Try more generic selectors
                            input_elem = container.find_element(By.CSS_SELECTOR, "input")
                            print("Found generic input element")
                        except Exception:
                            try:
                                input_elem = container.find_element(By.TAG_NAME, "textarea")
                                print("Found generic textarea element")
                            except Exception:
                                st.error(f"Could not locate input element for question {idx+1}")
                                print("Failed to find any input element for this question")
                
                if input_elem:
                    input_elem.clear()
                    input_elem.send_keys(answer)
                    print(f"Filled text answer: {answer}")
            except Exception as e:
                st.error(f"Error filling text question {idx+1}: {e}")
                print(f"Exception: {str(e)}")
    
    print("\n---------- Form filling completed ----------")
    return True

def login_to_google(driver, email, password):
    """
    Logs into Google account using the provided credentials.
    """
    try:
        # Navigate to Google login page
        driver.get("https://accounts.google.com/signin")
        time.sleep(2)
        
        # Take screenshot to show the login page
        screenshot = take_screenshot(driver)
        st.image(screenshot, caption="Login Page", use_column_width=True)
        
        # Enter email
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
        )
        email_input.clear()
        email_input.send_keys(email)
        email_input.send_keys(Keys.RETURN)
        time.sleep(2)
        
        # Take screenshot after email entry
        screenshot = take_screenshot(driver)
        st.image(screenshot, caption="Email Entered", use_column_width=True)
        
        # Enter password
        password_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        password_input.clear()
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)
        time.sleep(5)  # Wait for login to complete
        
        # Take screenshot after login attempt
        screenshot = take_screenshot(driver)
        st.image(screenshot, caption="Login Attempt Result", use_column_width=True)
        
        # Check if login was successful by looking for a common element on the Google account page
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-email]"))
            )
            return True
        except:
            # Check if we're no longer on the accounts.google.com/signin page
            if "accounts.google.com/signin" not in driver.current_url:
                return True
            # Check for possible 2FA prompt
            if "2-Step Verification" in driver.page_source or "verification" in driver.page_source.lower():
                st.warning("Two-factor authentication detected. Please complete it in the browser window.")
                return "2FA"
            return False
            
    except Exception as e:
        st.error(f"Error during login: {str(e)}")
        return False
def initialize_browser():
    """
    Initialize a Chrome browser with Docker-compatible settings
    """
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")  # Modern headless mode
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
    
    try:
        # First attempt: Try using webdriver-manager
        try:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            return driver
        except Exception as e1:
            st.warning(f"First browser initialization attempt failed: {e1}")
            
            # Second attempt: Try direct Chrome browser instance
            try:
                driver = webdriver.Chrome(options=chrome_options)
                return driver
            except Exception as e2:
                st.error(f"Second browser initialization attempt failed: {e2}")
                return None
    except Exception as e:
        st.error(f"Failed to initialize browser: {str(e)}")
        return None
# --- Streamlit App ---

st.title("Google Form Auto Filler with Gemini")
st.write("""
This app uses a headless browser to help you fill Google Forms automatically with AI-generated answers.
You'll be able to see screenshots of what's happening in the browser as it progresses.
""")

# Initialize session state variables
if "driver" not in st.session_state:
    st.session_state.driver = None
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
    # Initialize browser using our Docker-compatible function
            driver = initialize_browser()
            
            if driver:
                st.session_state.driver = driver
                
                # Show initial browser window
                screenshot = take_screenshot(driver)
                st.session_state.screenshot = screenshot
                st.image(screenshot, caption="Browser Started", use_column_width=True)
                
                # Try to login
                login_result = login_to_google(driver, email, password)
                st.session_state.login_status = login_result
                
                if login_result == True:
                    st.success("Login successful!")
                elif login_result == "2FA":
                    st.warning("Two-factor authentication may be required. Check the screenshot for verification prompts.")
                    st.info("You might need to complete 2FA in the browser window. Screenshots will update as you proceed.")
                else:
                    st.error("Login failed. Please check your credentials and try again.")
            else:
                st.error("Failed to initialize browser. Please check Docker configuration.")


# Add manual confirmation option for login
if st.session_state.login_status == False:
    st.info("If you can see that you're actually logged in from the screenshot above, click the button below:")
    if st.button("I'm actually logged in successfully"):
        st.session_state.login_status = True
        st.success("Login status manually confirmed! You can proceed to the form filling step.")

# Display a refreshing screenshot if 2FA is detected
if st.session_state.login_status == "2FA" and st.session_state.driver:
    if st.button("Take New Screenshot (for 2FA completion check)"):
        screenshot = take_screenshot(st.session_state.driver)
        st.session_state.screenshot = screenshot
        st.image(screenshot, caption="Current Browser State", use_column_width=True)
        
        # Check if we're past the login page now
        if "accounts.google.com/signin" not in st.session_state.driver.current_url:
            st.success("Looks like you completed 2FA! You can proceed to the form filling step.")
            st.session_state.login_status = True

if st.session_state.driver and (st.session_state.login_status == True or st.session_state.login_status == "2FA"):
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
            driver = st.session_state.driver
            
            # Only load the form if questions aren't already processed
            if "questions" not in st.session_state:
                driver.get(form_url)
                time.sleep(5)  # Allow the form to load completely
                
                # Show the form
                screenshot = take_screenshot(driver)
                st.image(screenshot, caption="Google Form Loaded", use_column_width=True)
                
                html = driver.page_source
                
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
                        driver.get(st.session_state.form_url)
                        time.sleep(3)
                        
                        if fill_form(driver, questions):
                            time.sleep(2)  # Give time for all fields to be properly filled
                            
                            # Take screenshot after filling
                            filled_screenshot = take_screenshot(driver)
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
                st.success("✅ Form has been filled with AI-generated answers!Just go and change your name and stuff")
                st.info("💡 You can check the answers generated by opening the form link on your browser.")
                st.markdown(f"📝 **Form Link:** [Open in Browser]({form_url})")

# Option to close the browser
if st.session_state.driver:
    st.markdown("---")
    if st.button("Close Browser"):
        st.session_state.driver.quit()
        st.session_state.driver = None
        st.session_state.login_status = None
        st.session_state.form_filled = False
        st.session_state.questions = None
        st.session_state.form_url = None
        st.session_state.filled_screenshot = None
        st.session_state.showing_filled_form = False
        st.success("Browser closed. All session data cleared.")