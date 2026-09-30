# Lab 2 Acoustic Sensing
link: https://youtu.be/nutYQACKp-U?si=_Bxgp_MH-suzfpxs 
## My interactive system
- Using acoustic sensing and a contact mic to be able to detect unique actions: Tap, Double Tap, Drag
- Once the virtual environment is set up and taps.py is running, Double Tap will initiate the App Switcher, Tap will go to the next app, Drag will go to the previous app, and Double Tapping again will confirm your selection

## How it works
- Takes FFT windows and looks for manually defined boundary cutoffs e.g.
    - For a Tap to be registered it is looking for a high spike (above -40dB) in frequency bin 0-200hz (in 1 time window)
    - For a Drag to be registered is is looking for a medium spike in the freq bin 0-200hz (between -80 to -40 dB) and a small spike in the 3000-5000hz range (between -100 to -80 dB). Additionally this needs to be read for 2 time windows 
- Double tap works off of a delay that once a Tap is read, it will wait to register the Tap until our Double Tap delay is up. If there is a second tap read during this time, it will instead be read as a Double Tap.
- Additionally includes logic for debounce time so multiple actions aren't registered from one attempted user input.
- Uses CGEvent API to send events via ctypes to register mac functions.