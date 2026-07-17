import {
  Home,
  History,
  User,
  LogOut,
  Database,
} from "lucide-react";

import "./Sidebar.css";


type SidebarProps = {
  activeMenu: string;
  onMenuChange: (menu: string) => void;
  onLogout: () => void;
};


const Sidebar = ({
  activeMenu,
  onMenuChange,
  onLogout,
}: SidebarProps) => {


  const menuItems = [
    {
      name: "Home",
      icon: Home,
    },
    {
      name: "History",
      icon: History,
    },
    {
      name: "Profile",
      icon: User,
    },
  ];


  return (

    <aside className="sidebar">


      {/* Brand */}

      <div className="sidebar-brand">

        <div className="logo-box">
          <Database size={22}/>
        </div>


        <div>
          <h2>
            Ingester
          </h2>

          <span>
            Platform
          </span>
        </div>

      </div>



      {/* Menu */}

      <nav className="sidebar-menu">


        {
          menuItems.map((item)=>{

            const Icon = item.icon;


            return (

              <button
                key={item.name}
                className={
                  activeMenu === item.name
                  ? "sidebar-item active"
                  : "sidebar-item"
                }

                onClick={()=>
                  onMenuChange(item.name)
                }
              >

                <Icon size={19}/>

                <span>
                  {item.name}
                </span>


              </button>

            );

          })
        }


      </nav>



      {/* Logout */}


      <button
        className="logout-btn"
        onClick={onLogout}
      >

        <LogOut size={19}/>

        <span>
          Logout
        </span>


      </button>


    </aside>

  );
};


export default Sidebar;